# prepare_firmware.py — сборка “скомпонованной” прошивки TXW/HGIC (SPI)

Скрипт берёт **входной `.bin`** (тело прошивки) и формирует **выходной образ**:

- **в начале файла** (с `0x0000`) — три заголовка подряд:
  1) `BOOT` (`hgic_spiflash_header_boot`)
  2) `SPI INFO` (`hgic_spiflash_header_spi_info`, `func_code=0x01`)
  3) `FW INFO` (`hgic_spiflash_header_firmware_info`, `func_code=0x02`)
- затем **паддинг** до смещения `CodeAddrOffset` (обычно `0x2000`)
- по адресу `CodeAddrOffset` — входной бинарник (код)

Скрипт считает CRC так же, как в исходниках HGIC:
- `CRC32` — как в `hgic_crc32()` (init `0xFFFFFFFF`, poly `0xEDB88320`, финальный `~crc`)
- `CRC16/MODBUS` — как в `hgic_crc16()` (init `0xFFFF`, poly `0xA001`)

---

## Требования

- Python 3.8+ (работает и на 3.13)
- Никаких внешних библиотек

---

## Файлы

- `prepare_firmware.py` — скрипт
- `makecode.ini` — **обязательно** лежит рядом со скриптом и всегда называется одинаково (см. `INI_NAME`)

---

## Запуск

В папке `pack` (где лежат `prepare_firmware.py` и `makecode.ini`):

```bat
python prepare_firmware.py RNode_Halow_Firmware.bin
