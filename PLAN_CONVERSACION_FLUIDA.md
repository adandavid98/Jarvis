# Plan de Mejoras: Conversación Fluida Full-Duplex, Interrupción (Barge-In), VAD Estable y Control de Cuota (`v0.2.0`)

## 1. Diagnóstico de los 4 Problemas Reportados

Tras revisar el código actual (`jarvis/live_session.py`, `jarvis/audio.py`, `jarvis/config.py`, `jarvis/bridge.py`) y la configuración del SDK `google-genai`:

| Problema Reportado | Causa Raíz Exacta en el Código |
| :--- | :--- |
| **1. Error `APIError: 1011 None. Resource has been exhausted`** | `LiveSession._send_mic_loop()` transmite frames de audio sin parar cada `80 ms` aunque estés en silencio o después de hablar, consumiendo tokens de audio en vivo continuamente. Además, en `bridge.py` (`_run_voice_loop`), cualquier excepción `1011` detiene el bucle de voz (`state = "stopped"`) sin reintento ni aviso claro. |
| **2. Repite *"Hola, ¿cómo te ayudo?"* y se activa solo en `idle_wake`** | Verificado directamente en `jarvis_memory.db` (`filas 72, 73, 81, 82, 83`): tras expirar los `15 s` de silencio (`idle_timeout`), el detector vuelve a dispararse solo a los **`4–14 s`** sin que nadie hable (`'Hola, ¿cómo te ayudo?'` repetido en bucle). Causas:<br>a) `wake_threshold = 0.18` dispara con **1 solo frame** (`80 ms`, `patience = 0`), permitiendo que un clic, golpecito o ruido ambiental amplificado hasta `10x` en `_preprocess_and_agc` supere `0.18`.<br>b) En `bridge.py`, `process_frame()` se ejecuta incluso cuando la compuerta de ruido está cerrada (`level_percent == 0`).<br>c) `self._resumption_handle` no se limpia al cerrar por `idle_timeout`, reinyectando `[Sistema: Responde brevemente...]` sobre el historial previo. |
| **3. Corta frases por la mitad / responde al instante al pausar** | Doble causa:<br>a) En `google.genai.types`, Gemini Live usa por defecto `END_SENSITIVITY_HIGH` (`EndSensitivity`), cerrando tu turno en micro-pausas al respirar porque `build_live_config()` no configura `realtime_input_config`.<br>b) En `jarvis/audio.py` (`_preprocess_and_agc`), el diferenciador `raw[n] - 0.94 * raw[n-1]` atenúa las vocales graves (`100–300 Hz`), no hay buffer *pre-roll* (corta la primera sílaba al empezar a hablar) y la compuerta baja la ganancia a `0.15x` cuando haces una breve pausa entre palabras. |
| **4. No permite interrumpir cuando Jarvis está hablando (`speaking`)** | `config.py` tiene `mute_mic_while_speaking: bool = True` y `live_session.py` descarta el 100% del micrófono mientras `audio_out.is_playing` es `True`. Jarvis queda completamente sordo mientras reproduce audio. |

---

## 2. Arquitectura Propuesta (Igual que Gemini Live App)

### Fase A — Captura de Voz Completa sin Cortes y Filtro Plano de Voz (`jarvis/audio.py`)
1. **Filtro Pasa-Altos Plano (`scipy.signal.butter` orden 2 a `85 Hz` con estado continuo `sosfilt_zi`):**
   - `scipy 1.17.1` ya está incluido en `.venv` (no requiere nuevas dependencias externas).
   - Corta ruido de ventilador/motor `< 85 Hz`, pero mantiene **ganancia plana `1.0` (`0 dB`) en toda la banda vocal (`100–7600 Hz`)** para que Gemini ASR transcriba todas las sílabas y palabras en español con nitidez natural.
   - *(Para el detector local `openWakeWord` podemos aplicar el pre-énfasis en una rama paralela solo para el wake word, manteniendo audio natural hacia Gemini Live).*
2. **Pre-Roll de `240 ms` (`3 frames`) + Hangover de `1.2 s` (`15 frames`):**
   - Un `collections.deque(maxlen=3)` guarda los últimos `240 ms` antes de disparar `is_voice`: al detectar inicio de palabra, se envían esos frames previos para **no perder la primera consonante/sílaba**.
   - `hangover` se amplía a `15 frames` (`1.2 s`) con transición suave (sin caer de golpe a `0.15x` en pausas cortas entre palabras).
   - El piso de ruido `_noise_rms` solo se actualiza en silencio real (`not is_voice and _hangover == 0`, tope máx. `160.0`) para impedir que la compuerta suba mientras hablas suave.

### Fase B — Conversación Fluida Full-Duplex con Interrupción / Barge-In (`jarvis/audio.py`, `jarvis/config.py`, `jarvis/live_session.py`)
1. **Activar escucha continua durante `speaking` (`mute_mic_while_speaking: bool = False` por defecto):**
   - Dejar de silenciar el micrófono cuando Jarvis está respondiendo.
2. **Cancelador de Eco Acústico Adaptativo (NLMS + Double-Talk Detector) para usar sin audífonos:**
   - `AudioOutputStream._callback` registrará la señal exacta que envía a la bocina (submuestreada de `24 kHz` a `16 kHz`) en un buffer circular compartido de referencia (`PlaybackReferenceBuffer`).
   - Cuando `audio_out.is_playing` es `True`, `AudioInputStream` alinea el retardo bocina→micrófono (`20–160 ms`) y ejecuta un filtro adaptativo **NLMS** que resta el eco de Jarvis antes de evaluar si el usuario está hablando:
     - **Si solo suena Jarvis (eco):** el residuo post-AEC queda bajo y se suprime; Jarvis NO se auto-interrumpe.
     - **Si el usuario habla encima (Barge-In / Double-Talk):** cuando `2 frames` seguidos (`160 ms`) superan el umbral de voz residual post-AEC (con o sin audífonos), `AudioInputStream` dispara el callback de interrupción local (`audio_out.clear()` instantáneo en `< 20 ms`), cambia estado a `listening` y envía esos frames + el *pre-roll* a Gemini.
3. **Configuración nativa de Barge-In y VAD paciente en `build_live_config()` (`jarvis/live_session.py`):**
   - Configurar `types.RealtimeInputConfig`:
     - `automatic_activity_detection = types.AutomaticActivityDetection(...)`:
       - `start_of_speech_sensitivity = types.StartSensitivity.START_SENSITIVITY_HIGH`
       - `end_of_speech_sensitivity = types.EndSensitivity.END_SENSITIVITY_LOW` *(evita que responda apenas haces una pausa)*
       - `prefix_padding_ms = 300`
       - `silence_duration_ms = 1100` *(espera ~1.1s de silencio real antes de responder)*
     - `activity_handling = types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS` *(al detectar voz del usuario, Gemini cancela su generación actual, emite `server_content.interrupted = True` y retoma la conversación con lo nuevo que dijiste)*
     - `turn_coverage = types.TurnCoverage.TURN_INCLUDES_ONLY_ACTIVITY`

### Fase C — Ahorro de Cuota Live API y Recuperación ante `1011 Resource Exhausted` (`jarvis/live_session.py`, `jarvis/bridge.py`)
1. **No quemar tokens en silencio (`audio_stream_end=True`):**
   - En `_send_mic_loop()`, transmitir a Gemini únicamente cuando hay actividad de voz (`is_voice` o dentro del `hangover`).
   - Al cerrarse el `hangover` tras terminar de hablar, enviar una señal única `await session.send_realtime_input(audio_stream_end=True)` y pausar el envío de silencio hasta la próxima palabra o *barge-in*. Esto reduce drásticamente el consumo de tokens/minuto (TPM) en sesiones largas.
2. **Manejo inteligente del error `1011` / Cuota Agotada (`jarvis/bridge.py`):**
   - Detectar errores `1011` / `Resource has been exhausted` / `429`:
     - Mostrar aviso claro en español (en lugar de `[Error] APIError: 1011 None...` técnico y quedarse muerto en `stopped`).
     - Limpiar `self.session_mgr._resumption_handle = None` si la sesión Live falló por cuota/desconexión.
     - Aplicar backoff progresivo (`4s -> 8s`) y regresar automáticamente al estado `idle_wake` (si está en modo wakeword) en vez de apagar el micrófono por completo.

### Fase D — Eliminar Repetición de *"Hola, ¿cómo te ayudo?"* (`jarvis/bridge.py`, `jarvis/live_session.py`)
1. **Saludo local instantáneo sin ensuciar el contexto de Gemini (o guard estricto):**
   - Al activar con `"Hey Jarvis"`, reproducir localmente un clip breve pre-generado/cacheado en `data/wake_greeting.wav` (o `System.Speech` cacheado una vez en WAV de 24 kHz) directamente por `AudioOutputStream`, **sin inyectar el prompt `[Sistema: Responde brevemente diciendo 'Hola, ¿cómo te ayudo?']` en cada sesión**.
   - Ventajas: respuesta en `< 30 ms`, **0 tokens de cuota**, y **imposible** que Gemini entre en bucle repitiendo `"Hola, ¿cómo te ayudo?"` por `session_resumption`.
2. **Limpieza de sesión y Cooldown Post-Sesión:**
   - Limpiar `self._resumption_handle = None` cuando termine por `idle_timeout` prolongado o `standby`, evitando heredar turnos fantasma entre conversaciones separadas.
   - Al volver a `idle_wake` en `bridge.py`, esperar a que `audio_out.is_playing` termine + `0.8 s` de *cooldown*, drenar `audio_in` y reiniciar `WakeWordDetector.reset()` para evitar auto-reactivación por eco.

### Fase E — Bloqueo de Falsas Activaciones de `"Hey Jarvis"` por Ruido Exterior (`idle_wake`)
1. **Confirmación Multi-Frame + Umbral Calibrado (`jarvis/wake_word.py`, `jarvis/config.py`):**
   - Subir `wake_threshold` de `0.18` a **`0.28`** (en las pruebas acústicas reales con micrófono de laptop, `"Hey Jarvis"` alcanza **`0.9866`** y sostiene `> 0.30` durante **4–5 frames seguidos**).
   - Exigir **`consecutive_hits >= 2` frames seguidos (`160 ms`)** sobre el umbral (o `score >= 0.60` instantáneo con alta energía vocal) antes de devolver `detected = True`, eliminando disparos falsos por ruidos breves, golpes o clics de un solo frame.
   - Si llega ruido bajo/silencio (`rms < min_voice_rms`), alimentar ceros en lugar de amplificar el ruido de sala, y reiniciar el contador de hits si cae debajo del umbral.
2. **Compuerta de Silencio Real para el Wake Word (`jarvis/audio.py`, `jarvis/bridge.py`):**
   - En `AudioInputStream._preprocess_and_agc`, cuando no haya voz activa ni hangover (`not is_voice and self._hangover == 0`), mantener la ganancia de salida de wake-word en silencio digital (`0` / `<= 0.05x`) y exigir que el pico no haya sido únicamente un impulso aislado (`< 2` frames) antes de amplificar a `8x–10x`.
   - En `JarvisBridge._run_voice_loop` (`idle_wake`):
     - Imponer un **enfriamiento (`2.0 s`) + drenaje continuo de `mic_queue`** después de que `AudioOutputStream.is_playing` termine al volver de cualquier sesión, reiniciando `self._detector.reset()` solo tras limpiar el buffer.
     - Si `not audio_in.is_speech_active` (compuerta cerrada por ruido exterior), pasar silencio digital al detector o descartar activación, impidiendo que ruido de sala/ventilador dispare el wake word cuando nadie habla.

---

## 3. Archivos a Modificar (cuando autorices ejecutar)

1. `jarvis/config.py`:
   - `mute_mic_while_speaking: bool = False` por defecto.
   - Ajustar `wake_threshold: float = 0.28` y `wake_consecutive_frames: int = 2`.
   - Nuevos parámetros ajustables: `silence_duration_ms: int = 1100`, `prefix_padding_ms: int = 300`, `barge_in_enabled: bool = True`.
2. `jarvis/wake_word.py`:
   - Confirmación multi-frame (`consecutive_frames=2`) + filtro de energía mínima para evitar falsos positivos por clics o ruido ambiental de 1 frame.
3. `jarvis/audio.py`:
   - `PlaybackReferenceBuffer` compartido entre `AudioOutputStream` y `AudioInputStream`.
   - `EchoCanceller` (NLMS + retardo acústico + Double-Talk Detector) para Barge-In sin audífonos.
   - Filtro Butterworth continuo (`scipy.signal.butter`) para Gemini + rama pre-énfasis para `openWakeWord`.
   - Ring buffer pre-roll (`240 ms`) + hangover `1.2 s` + propiedad `is_speech_active` para que `_send_mic_loop` envíe `audio_stream_end=True` en silencio y `idle_wake` ignore ruido exterior.
4. `jarvis/live_session.py`:
   - Configurar `RealtimeInputConfig` (`END_SENSITIVITY_LOW`, `START_SENSITIVITY_HIGH`, `silence_duration_ms=1100`, `START_OF_ACTIVITY_INTERRUPTS`).
   - Integrar Barge-In instantáneo (`audio_out.clear()` al detectar double-talk del usuario + `server_content.interrupted`).
   - Envío selectivo de audio (voz + pre-roll + `audio_stream_end=True` en pausa).
   - Saludo de wake local sin contaminar el contexto de Gemini.
5. `jarvis/bridge.py`:
   - Manejo y recuperación automática tras `1011 Resource has been exhausted`.
   - Cooldown de `2.0 s` + drenaje completo tras salir de sesión antes de rearmar `WakeWordDetector`, respetando `audio_in.is_speech_active`.
6. `tests/test_jarvis.py`:
   - Pruebas unitarias para: rechazo de ruido/impulsos en `WakeWordDetector` (`consecutive_frames=2`, `threshold=0.28`), `RealtimeInputConfig` en `build_live_config`, NLMS echo suppression + detección de double-talk (barge-in), pre-roll sin pérdida de sílabas iniciales, ahorro de cuota en silencio (`audio_stream_end`), y recuperación de `1011`.
7. Empaquetado y sincronización (`AGENTS.md`):
   - Actualizar `requirements.txt` si aplica, subir `desktop/package.json` a `0.2.0`, reconstruir `desktop/dist/Jarvis-Setup.exe`, actualizar instalación local y subir a GitHub.

---

## 4. Plan de Verificación

1. **Pruebas unitarias (`pytest`):** Verificar los nuevos tests de rechazo de ruido en `idle_wake`, VAD, pre-roll, AEC/barge-in, `RealtimeInputConfig` y manejo de error `1011`.
2. **Prueba de falsos positivos en silencio/ruido (`60 s`):** Dejar `WakeWordDetector` + `AudioInputStream` escuchando ruido ambiental, teclado y golpes de mesa durante `60 s` y confirmar **`0` activaciones falsas**, mientras `"Hey Jarvis"` por bocina sigue disparando con `> 0.85`.
3. **Prueba acústica de Barge-In por bocinas:** Reproducir audio simulando a Jarvis hablando por `Speakers (Realtek(R) Audio)` y validar que durante el solo eco no dispara interrupción, pero al hablar encima corta `audio_out` en `< 200 ms` y deja pasar la voz limpia.
4. **Prueba en vivo de frases largas con pausas:** Verificar que pausas de `0.6–0.8 s` entre cláusulas no cortan la oración y se transcriben completas.
5. **Prueba de `"Hey Jarvis"` sin repetición:** Verificar saludo único, cooldown tras salir y `0%` de consumo en silencio tras `audio_stream_end`.
