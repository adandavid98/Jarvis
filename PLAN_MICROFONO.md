# Plan de Diagnóstico y Solución: Micrófono Nativo de Windows y Conmutación Automática (Con/Sin Audífonos)

## 1. Diagnóstico y Pruebas Realizadas en esta Laptop

Se ejecutaron pruebas acústicas, de espectro (FFT), de niveles CoreAudio COM (`IMMDeviceEnumerator` / `IAudioEndpointVolume` / `IDeviceTopology`), de `openWakeWord` y de `Gemini Live` en la laptop actual (`Lenovo / Realtek(R) Audio ALC257 con Intel SST`):

| Prueba Realizada | Resultado Observado | Causa Raíz Identificada |
| :--- | :--- | :--- |
| **1. Inspección de `.env` en `%APPDATA%\jarvis-desktop\.env`** | Contenía `JARVIS_INPUT_DEVICE=9` fijado por el selector del HUD. | Al conectar/desconectar audífonos (`ADAN's Buds3`, `Beats Studio Pro`), los índices de PortAudio cambian y el índice fijo deja de coincidir con el micrófono activo. |
| **2. Volumen Nativo en Windows CoreAudio (`IAudioEndpointVolume`)** | El endpoint `Microphone Array (Realtek(R) Audio)` estaba en **`47.2% (-0.2 dB)`** (rango `-17 dB` a `+20 dB`). | En `-0.2 dB`, el arreglo digital MEMS entregaba `peak = 14`, `rms = 1.99` (casi silencio total). Al subirlo a **`100% (+20.0 dB)`** con `+10 dB` de amplificación de hardware, la señal subió **35x–68x** (`peak = 475–1024`, `rms = 68–78`). |
| **3. Prueba WASAPI `16 kHz / 24 kHz Mono`** | `sd.InputStream(samplerate=16000, channels=1, device=9)` fallaba con `PortAudioError` sin `WasapiSettings`. | Windows WASAPI requiere `sd.WasapiSettings(auto_convert=True)` (`AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM`) para convertir automáticamente la frecuencia nativa (`48 kHz` estéreo) a `16 kHz` mono en el kernel de audio de Windows. |
| **4. Espectro FFT y Atenuación por Distancia (Sin Audífonos)** | Sin audífonos (a 50–70 cm de la pantalla), hay ruido sub-grave de ventilador/red (`9–67 Hz`) y la voz llega con picos de `500–1,800` (vs `12,000+` con audífonos a 2 cm). | Se requiere un **Filtro Pasa-Altos (>100 Hz)** para eliminar el retumbo grave y **Control Automático de Ganancia (Software AGC)** para amplificar la voz de laptop hasta `~12,000` sin saturar. |
| **5. Prueba de `"Hey Jarvis"` con pronunciación en Español (`es-MX`)** | Con voz en español (`Microsoft Sabina Desktop`), `"Hey Jarvis"` obtuvo `score = 0.2876` y `"Ey Jarvis"` obtuvo `0.1891` (mientras el silencio da `0.0002–0.0043`). | El umbral por defecto `wake_threshold = 0.50` bloqueaba la activación al hablar con acento en español o desde el micrófono de la laptop. Bajando el umbral a **`0.18`** (con AGC activo), `"Hey Jarvis"` se activa de inmediato sin falsos positivos. |
| **6. Cambio en caliente (Hot-Plug) Con/Sin Audífonos** | Al desconectar los audífonos mientras Jarvis ya estaba corriendo, el stream de PortAudio permanecía atado al dispositivo anterior. | Se requiere un monitor automático de cambios del dispositivo predeterminado de Windows que recargue PortAudio y reconecte el stream de entrada y salida en caliente. |

---

## 2. Plan de Implementación

1. **Eliminar el Selector de Micrófonos del HUD (`desktop/renderer/index.html`, `app.js`, `styles.css`)**:
   - Retirar `<select id="mic-select">` y `<button id="btn-refresh-mic">`, dejando únicamente el medidor visual en vivo (`MIC LEVEL` + `#mic-bar`).
   - Limpiar `JARVIS_INPUT_DEVICE` de `%APPDATA%\jarvis-desktop\.env`.
2. **Auto-Unmute, Boost 100%, `WasapiSettings(auto_convert=True)`, Filtro Pasa-Altos y Software AGC (`jarvis/audio.py`)**:
   - Asegurar que el micrófono activo de Windows no esté muteado y tenga volumen `100%` en CoreAudio.
   - Usar `sd.WasapiSettings(auto_convert=True)` para capturar a `16 kHz` y reproducir a `24 kHz` en los dispositivos predeterminados de Windows WASAPI.
   - Aplicar filtro pasa-altos (>100 Hz) y ganancia adaptativa (AGC) para voz a distancia en laptop.
   - Soportar `hot-swap` automático cuando Windows cambie de micrófono o altavoz (al conectar o desconectar audífonos).
3. **Detección Sensible de `"Hey Jarvis"` (`jarvis/wake_word.py`, `jarvis/config.py`, `jarvis/bridge.py`)**:
   - Ajustar `wake_threshold` a `0.18` y evaluar frames con ganancia normalizada en `WakeWordDetector`.
   - Ejecutar watchdog de dispositivos de audio cada `2.0s` en `JarvisBridge`.
