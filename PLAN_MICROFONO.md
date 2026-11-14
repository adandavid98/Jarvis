# Plan de Diagnóstico y Solución: Micrófono Nativo de Windows y Bloqueador de Ruido Exterior (`v0.1.9`)

## 1. Diagnóstico y Pruebas Realizadas en esta Laptop

Se ejecutaron pruebas acústicas con reproducción real por bocinas (`test_hj.wav` con `"Hey Jarvis"` y tonos de 1 kHz), análisis FFT por canal y capturas comparativas entre **MME**, **DirectSound**, **WASAPI Shared** y **WASAPI Exclusive** en la laptop (`Lenovo / Realtek(R) Audio ALC257 con Intel SST`):

| Prueba Realizada | Resultado Observado | Causa Raíz Identificada |
| :--- | :--- | :--- |
| **1. Comparativa MME / DirectSound / WASAPI Shared vs Exclusive** | En `WASAPI Shared` (`Dev 9`, `16 kHz` o `48 kHz`), tras los primeros `0.3s` de ruido inicial (`rms = 62–128` que movía `#mic-bar`), la señal cae a `rms = 2.2–9.5` y `openWakeWord` marca **`0.0006–0.0071`** (no despierta). | El APO propietario de Realtek (`FxProperties` en modo compartido de Windows) suprime y distorsiona la voz ambiental a distancia y el canal `Ch 0` del arreglo MEMS no responde acústicamente. En cambio, **WASAPI Exclusive (`device=9`, `48 kHz`, `2 ch`)** lee la cápsula física viva `Ch 1` sin pérdida (`peak = 7,565`). |
| **2. Continuidad de Fase y Pre-énfasis Compensado por Frecuencia** | El filtrado FFT por bloques de 80 ms introducía discontinuidades de fase y cortaba consonantes finales (`"-vis"`), bajando el score a `0.006`. Con filtro pasa-altos continuo de 1er orden (`pre_scale = 2.5` a `48 kHz`, `3.8` a `16 kHz`) + diezmado anti-aliasing 3-tap (`48 kHz -> 16 kHz`), `openWakeWord` alcanzó **`0.9939` (`99.4%`)**. | Mantener estado continuo (`self._prev_sample`) y diezmar con promedio de 3 muestras preserva la envolvente temporal y armónicos de la voz humana mientras atenúa ventilador/zumbido `< 300 Hz`. |
| **3. Bloqueador de Ruido Exterior con Hangover de Voz (`640 ms`)** | El AGC anterior fijaba umbral bajo (`110`), confundiendo el ruido de sala con voz y manteniendo el indicador `#mic-bar` en `15%–27%` continuamente. | Con umbral adaptativo de piso de ruido (`self._noise_rms`) + **8 frames de hangover (`640 ms`)**, el medidor `#mic-bar` queda en **`0%` durante el silencio/ruido de fondo** y solo se abre al detectar voz humana real, conservando las consonantes finales para Gemini Live y `openWakeWord`. |
| **4. Volumen Nativo CoreAudio (`IAudioEndpointVolume`) & Fallback** | El endpoint predeterminado de Windows puede estar en `< 90%` o muteado. Además, si otra app (como WhatsApp) retiene el micrófono en uso exclusivo, debe existir respaldo automático. | `get_windows_default_endpoints(ensure_mic_unmuted=True)` garantiza `unmute` y volumen `100%` (+20 dB). Si WASAPI Exclusive lanza error por contención con otra app, `AudioInputStream` cae suavemente a `WASAPI Shared (auto_convert=True)` y luego `MME`. |

---

## 2. Cambios Implementados en `v0.1.9`

1. **Captura Prioritaria en WASAPI Exclusive Nativo + Selección de Cápsula Viva (`jarvis/audio.py`)**:
   - `_open_portaudio_stream()` intenta primero **WASAPI Exclusive** a la frecuencia nativa (`48 kHz`, `2 canales`), pasando luego a **WASAPI Shared (`auto_convert=True`)** (para audífonos/Bluetooth o cuando otra app usa el micrófono) y por último **MME**.
   - En arreglos multi-canal, `_callback()` selecciona el canal con mayor desviación estándar AC (`np.std`), ignorando cápsulas muertas como `Ch 0` en Realtek ALC257.
2. **Bloqueador de Ruido Exterior + AGC Adaptativo con Hangover (`jarvis/audio.py`)**:
   - Filtro continuo pre-énfasis pasa-altos (`< 300 Hz`) con compensación de ganancia por frecuencia de muestreo (`2.5` para `> 24 kHz`, `3.8` para `<= 24 kHz`) y diezmado anti-aliasing `48 kHz -> 16 kHz`.
   - Seguimiento adaptativo del piso de ruido (`_noise_rms`), compuerta enfocada en voz (`is_voice`), y ventana de retención de `8 frames (640 ms)` (`_hangover`) para no truncar palabras ni consonantes.
   - `level_percent` ahora reporta `0%` en ruido ambiental de sala y responde únicamente ante voz humana.
3. **Empaquetado y Sincronización (`desktop/package.json`, `data/SYNC.md`, `desktop/dist/Jarvis-Setup.exe`)**:
   - Versión incrementada a **`0.1.9`**, reconstrucción de `jarvis-bridge.exe` + `Jarvis-Setup.exe`, actualización local y publicación en GitHub.
