# RNode-Halow

[🇬🇧 English](#english) | [🇷🇺 Русский](#русский)


Coverage map: https://map.rnode-halow.ru/

Firmware flasher: https://github.com/I-AM-ENGINEER/RNode_Halow_OTA_Flasher

---

## English

### Implemented Features

The following is currently implemented:

- Compatible with reticulum and any client (Meshchat, Sideband, Columba, Nomandnet, etc...) 
- Broadcast packet transmission and reception LMAC WiFi HaLow
- Unicast with retries for Link packages (auto detection)
- DHCP client or static IP
- Real-time statistics
- Frequency and modulation parameter selection
- OTA firmware update from web page
- LBT with CCA and airtime limit
- SLIP for connection over UART
- Integration with MQTT for transmit node install position and statistics
- Auto rate adaptation based on signal level

### What Is Currently Missing

- USB, SPI connection support not implemented
- No DHCP server for easy connection with limited devices (phones with usb-ethernet)
- MCS10 isnt stable enough

### Default Parameters

- **Frequency:** 864–865 MHz (1 MHz channel width)
- **PHY:** WiFi MCS0
- **Power:** 14 dBm (25 mW)
- **TCP Port:** 4242

### Device Settings

Device can be configured with HTTP web page on port 80

### Reticulum Configuration

Add the following to your Reticulum interfaces config. The IP address can be found via your router's DHCP server — the device hostname is `RNode-Halow-XXXXXX`, where `XXXXXX` is the last 3 bytes of the MAC address, or via `RNode-HaLow Flasher.exe`.

    [[RNode-Halow]]
      type = TCPClientInterface
      enabled = yes
      target_host = 192.168.XXX.XXX
      target_port = 4242

### For Developers

To get started quickly, install the Taixin CDK and open the project in the `project` folder. All necessary tooling is included in CDK.

Logs are output via UART (IO12, IO13) at **2,000,000 baud** (blocking logs).

For full debugging, use a Blue Pill flashed as CKLink or Chinese CKLink clones. The chip **must** be STM32F103C8 — C6 will not work, and Chinese suppliers often ship rejected/cloned chips with broken USB.

#### Flashing via CKLink (EIDE / CLI)

Requirements: DebugServer + GDB, CKLink debugger connected.

```bash
python utils/flash.py                        # flash firmware
python utils/flashlog.py --port COM3 -n 500  # flash + capture boot logs
```

Tool paths are read from env vars `CSKY_DEBUGSERVER`, `CSKY_GDB` (and `CSKY_MINGW_BIN` on Windows). Defaults are in `utils/flash_env.sh`.

OTA firmware is generated automatically at `project/out/XXX.tar` after building the project.

---

## Русский

### Реализованный функционал

На текущий момент реализовано следующее:

- Совместимость с Reticulum и любыми клиентами (Meshchat, Sideband, Columba, NomadNet и т.д.)
- Передача и прием широковещательных пакетов по LMAC WiFi HaLow
- Unicast с повторными передачами для Link-пакетов (автоопределение)
- DHCP-клиент или статический IP
- Статистика в реальном времени
- Выбор частоты и параметров модуляции
- OTA-обновление прошивки с веб-страницы
- LBT с CCA и ограничением airtime
- SLIP для подключения через UART
- Интеграция с MQTT для передачи позиции установки ноды и статистики
- Автоматическая адаптация скорости (rate adaptation) по уровню сигнала

### Что на данный момент отсутствует

- Не реализована поддержка подключения по USB и SPI
- Нет DHCP-сервера для простого подключения ограниченных устройств (телефоны с usb-ethernet)
- MCS10 недостаточно стабилен

### Стандартные параметры

- **Частота:** 864–865 МГц (ширина канала 1 МГц)
- **PHY:** WiFi MCS0
- **Мощность:** 14 dBm (25 мВт)
- **TCP-порт:** 4242

### Настройки устройства

Устройство настраивается через HTTP веб-страницу на порту 80

<img width="1492" height="2102" alt="Screenshot_1" src="https://github.com/user-attachments/assets/9a15407e-d534-4f44-bf0b-7dbb65177d5f" />

### Настройка Reticulum через конфиг

Добавьте в конфиг интерфейсов Reticulum новый TCPClientInterface.

IP-адрес можно узнать через DHCP-сервер на роутере — устройство имеет hostname `RNode-Halow-XXXXXX`, где `XXXXXX` — последние 3 байта MAC-адреса, или через [RNode-HaLow Flasher.exe](https://github.com/I-AM-ENGINEER/RNode_Halow_Firmware/releases/).

```
  [[RNode-Halow]]
    type = TCPClientInterface
    enabled = yes
    target_host = 192.168.XXX.XXX
    target_port = 4242
```

### Для разработчиков

Для простого старта достаточно поставить Taixin CDK и открыть проект в папке "project". Весь необходимый инструментарий содержится в CDK.

Логи идут по UART (IO12, IO13) со скоростью 2'000'000 бод, т.к. логи блокирующие

Для полноценной отладки используется Blue Pill прошитая в CKLink. Чип обязательно должен быть STM32F103C8, C6 не подойдет + китайцы любят пихать отбраковку/клоны с неработающим USB.

### Прошивка через CKLink (EIDE / CLI)

Требования: DebugServer + GDB, подключенный CKLink.

```bash
python utils/flash.py                        # прошить прошивку
python utils/flashlog.py --port COM3 -n 500  # прошить + захватить логи загрузки
```

Пути к инструментам берутся из переменных окружения `CSKY_DEBUGSERVER`, `CSKY_GDB` (и `CSKY_MINGW_BIN` на Windows). По умолчанию — из `utils/flash_env.sh`.

Прошивка для OTA генерируется автоматически `project/out/XXX.tar` после сборки проекта.

## Support

USDT (TON) `EQCPUzAUHbm7M_6tYwQhC7DGNMpouiR_SY-6Bw_FguEaVZZp`

Litecoin `LSzupUjU2WtMAXkrE87icEJHaQes7JaXqA`
