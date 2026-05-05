# BabyMonitor

Sistema de monitoramento de bebê baseado em dois Raspberry Pi: um **Pi Câmera** que captura vídeo, detecta choro e serve a stream, e um **Pi Monitor** que conecta à câmera e exibe a interface em modo kiosk (Chromium tela cheia).

```
[Pi Câmera] ──── WiFi AP (10.42.0.1) ────► [Pi Monitor]
  libcamera                                   Chromium
  GStreamer HLS                               HLS player
  Detector de choro                           WebSocket alerts
  API REST :8080
```

---

## Requisitos de hardware

| Componente | Pi Câmera | Pi Monitor |
|---|---|---|
| Modelo recomendado | Raspberry Pi Zero 2W ou Pi 4 | Raspberry Pi 4 ou Pi 3B+ |
| Câmera | Módulo Camera v2/v3 (libcamera) ou webcam USB | — |
| Microfone | USB ou HAT de áudio (opcional) | — |
| Display | — | HDMI |
| Sistema operacional | Raspberry Pi OS Lite (64-bit) | Raspberry Pi OS (64-bit) |

---

## Instalação

### Pi Câmera

1. Clone o repositório no Pi Câmera:

```bash
git clone <url-do-repositorio> ~/App
cd ~/App
```

2. Execute o script de instalação como root:

```bash
sudo bash scripts/setup_camera.sh
```

O script instala automaticamente:
- Python 3, pip e dependências Python (`requirements.txt`)
- GStreamer com suporte a libcamera, H.264 e HLS
- PortAudio e PyAudio para detecção de choro
- NetworkManager e Avahi (mDNS)
- Serviço systemd `babymonitor-camera`

3. Edite as credenciais de WiFi fallback (opcional):

```bash
sudo nano /etc/babymonitor/camera.yaml
```

```yaml
fallback_wifi:
  ssid: "NomeDaSuaRede"
  password: "SuaSenha"
```

4. Verifique o serviço:

```bash
sudo systemctl status babymonitor-camera
sudo journalctl -u babymonitor-camera -f
```

---

### Pi Monitor

1. Clone o repositório no Pi Monitor:

```bash
git clone <url-do-repositorio> ~/App
cd ~/App
```

2. Execute o script de instalação como root:

```bash
sudo bash scripts/setup_monitor.sh
```

O script instala automaticamente:
- Python 3, pip e dependências Python (`pyyaml`, `zeroconf`)
- NetworkManager e Avahi (mDNS)
- Chromium, Xorg e Openbox (interface kiosk)
- Serviços systemd `babymonitor-monitor` e `babymonitor-kiosk`

3. Edite o WiFi fallback se necessário:

```bash
sudo nano /etc/babymonitor/monitor.yaml
```

4. Verifique os serviços:

```bash
sudo systemctl status babymonitor-monitor babymonitor-kiosk
sudo journalctl -u babymonitor-monitor -f
```

---

## Configuração

### Pi Câmera — `/etc/babymonitor/camera.yaml`

```yaml
ap:
  ssid: BabyMonitor-AP      # Nome da rede WiFi criada pela câmera
  password: babymonitor123  # Senha da rede
  ip: 10.42.0.1             # IP da câmera no modo AP

fallback_wifi:
  ssid: ""       # WiFi da casa (usado se o modo AP falhar)
  password: ""

server:
  host: 0.0.0.0
  port: 8080

streaming:
  width: 1280
  height: 720
  framerate: 30
  hls_dir: /tmp/hls
  hls_target_duration: 1   # Duração de cada segmento HLS em segundos
  hls_max_files: 5         # Número máximo de segmentos mantidos

recordings:
  output_dir: /opt/babymonitor/recordings
  max_recordings: 50       # Gravações mais antigas são apagadas automaticamente
  min_free_mb: 500         # Gravação recusada abaixo deste espaço livre (MB)

cry_detector:
  sample_rate: 16000
  chunk_size: 2048
  threshold: 0.65          # Confiança mínima para detectar choro (0.0–1.0)
  silence_timeout: 10      # Segundos de silêncio para encerrar detecção
  calibrate_on_start: true # Calibra ruído ambiente ao iniciar

security:
  api_token: ""            # Token para endpoints POST (vazio = sem autenticação)
```

### Pi Monitor — `/etc/babymonitor/monitor.yaml`

```yaml
fallback_wifi:
  ssid: ""       # WiFi da casa
  password: ""

camera_ap:
  ssid: BabyMonitor-AP      # Deve coincidir com a config da câmera
  password: babymonitor123

kiosk_url_file: /etc/babymonitor/kiosk_url
default_camera_url: http://10.42.0.1:8080
```

---

## API REST (Pi Câmera — porta 8080)

### Endpoints públicos (GET)

| Endpoint | Descrição |
|---|---|
| `GET /` | Interface web |
| `GET /stream/live.m3u8` | Playlist HLS ao vivo |
| `GET /stream/{segment}` | Segmentos de vídeo `.ts` |
| `GET /api/recordings` | Lista gravações disponíveis |
| `GET /api/recordings/{filename}` | Download de uma gravação |
| `GET /api/status` | Status atual da gravação |
| `GET /api/health` | Health check (stream, detector, disco) |
| `WS /ws/alerts` | WebSocket para alertas de choro em tempo real |

### Endpoints protegidos (POST)

Requerem o header `X-Api-Token` se `security.api_token` estiver configurado.

| Endpoint | Descrição |
|---|---|
| `POST /api/recording/start` | Inicia gravação manual |
| `POST /api/recording/stop` | Para gravação manual |
| `POST /api/wifi/configure` | Atualiza credenciais WiFi fallback |

Exemplo:
```bash
curl -X POST http://10.42.0.1:8080/api/recording/start \
     -H "X-Api-Token: seu-token"
```

---

## Modos de operação de rede

### Modo P2P (padrão)

O Pi Câmera cria uma rede WiFi (`BabyMonitor-AP`). O Pi Monitor conecta diretamente a ela. Sem necessidade de roteador.

```
Pi Câmera (AP 10.42.0.1) ◄──── Pi Monitor (cliente)
```

### Modo WiFi fallback

Se o modo AP falhar, ambos os dispositivos conectam à rede WiFi configurada em `fallback_wifi`. O Pi Monitor descobre o IP da câmera via mDNS (`_babymonitor._tcp.local.`).

```
Roteador WiFi ◄──── Pi Câmera  (anuncia via mDNS)
              ◄──── Pi Monitor (descobre via mDNS)
```

---

## Detecção de choro

A detecção funciona sem modelos de ML, usando análise espectral e autocorrelação:

1. Amostra áudio continuamente via PyAudio
2. Analisa energia na banda de 300–2000 Hz (faixa vocal infantil)
3. Verifica periodicidade via autocorrelação (padrão de choro: bursts a cada 0,5–2s)
4. Se `confidence >= threshold`: inicia gravação e envia alerta via WebSocket
5. Após `silence_timeout` segundos de silêncio: encerra gravação

> **Sem microfone:** se nenhum dispositivo de entrada de áudio for encontrado, a detecção de choro é desabilitada automaticamente e o sistema continua operando normalmente (streaming e API funcionam).

---

## Estrutura de arquivos instalados

```
/opt/babymonitor/
├── babymonitor/        # Código Python
└── web/                # Frontend (HTML, JS, CSS)

/etc/babymonitor/
├── camera.yaml         # Config da câmera
├── monitor.yaml        # Config do monitor
└── kiosk_url           # URL gerada pelo monitor para o kiosk

/opt/babymonitor/recordings/   # Gravações de vídeo (.mp4)
/tmp/hls/                      # Segmentos HLS ao vivo (live.m3u8, seg*.ts)

/etc/systemd/system/
├── babymonitor-camera.service
├── babymonitor-monitor.service
└── babymonitor-kiosk.service
```

---

## Variáveis de ambiente

| Variável | Padrão | Descrição |
|---|---|---|
| `CAMERA_CONFIG` | `/etc/babymonitor/camera.yaml` | Caminho do arquivo de config da câmera |
| `LOG_LEVEL` | `INFO` | Nível de log (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `LOG_FILE` | — | Caminho para arquivo de log (opcional) |

---

## Comandos úteis

```bash
# Ver logs em tempo real
sudo journalctl -u babymonitor-camera -f
sudo journalctl -u babymonitor-monitor -f

# Reiniciar serviços
sudo systemctl restart babymonitor-camera
sudo systemctl restart babymonitor-monitor babymonitor-kiosk

# Verificar stream HLS
ls -lh /tmp/hls/

# Health check da API
curl http://10.42.0.1:8080/api/health

# Listar gravações
curl http://10.42.0.1:8080/api/recordings

# Desinstalar completamente
sudo bash scripts/uninstall.sh
```

---

## Solução de problemas

**Serviço não inicia / reinicia em loop**
```bash
sudo journalctl -u babymonitor-camera -n 50 --no-pager
```

**Erro `OSError: [Errno -9996] Invalid input device`**
Microfone não encontrado. A detecção de choro é desabilitada automaticamente; o streaming continua funcionando.

**`/tmp/hls/` vazio — sem stream**
```bash
# Verificar se a câmera está reconhecida
libcamera-hello --list-cameras
v4l2-ctl --list-devices
```

**Monitor não conecta à câmera**
```bash
# Verificar se a rede AP está ativa
nmcli device status
nmcli connection show

# Verificar mDNS
avahi-browse -r _babymonitor._tcp
```

**API inacessível (porta 8080)**
```bash
curl http://10.42.0.1:8080/api/health
sudo ss -tlnp | grep 8080
```
