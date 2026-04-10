# UniDesk

Software KVM switch přes lokální síť — sdílej klávesnici a myš z jednoho PC na ostatní pouhým přejetím kurzoru přes hranici monitoru.

Funguje na Windows, nevyžaduje žádný hardware. Inspirováno nástroji jako Barrier / Synergy, ale napsáno od nuly v Pythonu.

---

## Co to dělá

- **Hlavní stanice (PC1)** má fyzickou klávesnici a myš. Spustíš na ní server.
- **Ostatní stanice (PC2, PC3…)** spustí klienta a připojí se po síti.
- V GUI na PC1 si přetáhneš virtuální monitor PC2 vedle svých fyzických monitorů.
- Jakmile přejedeš myší přes tu hranici, kurzor na PC1 zmizí a ovládáš PC2 — klávesnice i myš.
- Zpět přejedeš myší na druhou stranu — ovládáš zase PC1.
- Schránka (Ctrl+C) se automaticky synchronizuje mezi oběma PC.

### Přepínání ovládání

| Situace | Co se stane |
|---|---|
| Myš na PC1 | Klávesnice + myš ovládají PC1 |
| Myš přejede na virtuální monitor | Kurzor zmizí z PC1, ovládáš PC2 |
| Uživatel na PC2 fyzicky pohne myší | PC2 převezme lokální ovládání |
| Myš se vrátí zpět na PC1 | PC1 dostane ovládání zpět |

---

## Požadavky

- **Windows 10/11** (obě PC)
- **Python 3.10+**
- Obě PC ve stejné lokální síti
- Admin práva **nejsou potřeba** pro běžné použití

> **Poznámka k admin právům:** Low-level hooky (`WH_MOUSE_LL`, `WH_KEYBOARD_LL`) a `SendInput` fungují bez admin práv pro normální okna. Pokud chceš ovládat UAC dialogy nebo Task Manager, spusť server jako správce. Klienta lze povýšit automaticky pomocí přepínače `--admin`.

---

## Instalace

```bash
pip install PyQt6 pywin32
```

---

## Použití

### 1. Zjisti IP adresu PC1

Na PC1 spusť v příkazovém řádku:
```
ipconfig
```
Hledej `IPv4 Address` — např. `192.168.1.10`.

### 2. Spusť server na PC1 (hlavní stanice)

```bash
python main_server.py
```

Otevře se GUI okno s rozložením monitorů. Server naslouchá na portu `25432`.

### 3. Spusť klienta na PC2

```bash
python main_client.py --server 192.168.1.10
```

Nahraď `192.168.1.10` IP adresou PC1. Na PC2 se zobrazí ikona v systémové liště — zelená znamená připojeno.

### Argumenty příkazové řádky

#### Server (`main_server.py`)
| Argument | Typ | Výchozí | Popis |
|---|---|---|---|
| `--port` | `int` | `25432` | TCP port, na kterém server naslouchá. |
| `--sensitivity` | `float` | `1.0` | Násobič citlivosti myši. Slouží k dorovnání DPI mezi počítači. |
| `--scale-to-snap` | flag | `vypnuto` | Škálovat virtuální zónu tak, aby přesně odpovídala hraně fyzického monitoru. |
| `--hide-mouse` | flag | `vypnuto` | Při předání kontroly klientovi se myš na serveru teleportuje do pravého dolního rohu. |
| `--debug` | flag | `vypnuto` | Zapne detailní ladicí (debug) logování. |
| `--shutdown` | `int` | `0` | Automaticky vypne server po N sekundách (užitečné pro ladění). |

#### Klient (`main_client.py`)
| Argument | Typ | Výchozí | Popis |
|---|---|---|---|
| `--server` | `str` | **Povinné** | IP adresa nebo hostname UniDesk serveru. |
| `--port` | `int` | `25432` | TCP port serveru. |
| `--admin` | flag | `vypnuto` | Vyžádá práva správce (umožní klikat na hlavní lištu / Start). |
| `--hide-mouse` | flag | `vypnuto` | Při vrácení kontroly serveru se myš na klientovi teleportuje do pravého dolního rohu. |

---

### 4. Nastav rozložení monitorů

V GUI na PC1 (tab **Monitor Layout**):
- Šedé bloky = fyzické monitory PC1
- Barevný blok = virtuální monitor PC2

Přetáhni barevný blok na hranu některého ze šedých bloků — vlevo, vpravo, nahoru nebo dolů. Blok se přichytí k hraně.

Od teď, když přejedeš myší přes tuto hranici, začneš ovládat PC2.

---

## Struktura projektu

```
UniDesk/
├── main_server.py              # Spustit na PC1
├── main_client.py              # Spustit na PC2
├── requirements.txt
└── unidesk/
    ├── common/
    │   ├── protocol.py         # Síťový protokol (length-prefixed JSON)
    │   ├── config.py           # Sdílené datové třídy (MonitorRect, VirtualPlacement)
    │   └── constants.py        # Porty, timeouty, konstanty
    ├── server/
    │   ├── server_app.py       # Orchestrace serveru
    │   ├── input_capture.py    # Win32 low-level hooky — zachytává klávesnici a myš
    │   ├── monitor_info.py     # Detekce fyzických monitorů (EnumDisplayMonitors)
    │   ├── edge_detector.py    # Logika přechodu kurzoru na virtuální monitor
    │   ├── client_manager.py   # Správa připojených klientů
    │   └── clipboard_server.py # Sledování schránky (WM_CLIPBOARDUPDATE)
    ├── client/
    │   ├── client_app.py       # Orchestrace klienta
    │   ├── input_simulator.py  # Win32 SendInput — simulace myši a klávesnice
    │   ├── cursor_manager.py   # Skrývání kurzoru, detekce fyzického pohybu myši
    │   ├── clipboard_client.py # Sync schránky na straně klienta
    │   └── monitor_info_client.py
    └── gui/
        ├── main_window.py      # Hlavní okno (PyQt6), 3 taby
        ├── monitor_layout.py   # Drag-and-drop rozložení monitorů (QGraphicsScene)
        ├── client_list.py      # Seznam připojených klientů
        └── tray_icon.py        # Ikona v systémové liště
```

---

## Jak to funguje uvnitř

### Síťový protokol

TCP spojení, port `25432`. Každá zpráva je length-prefixed JSON:

```
[4 bajty: délka zprávy (big-endian uint32)][N bajtů: UTF-8 JSON]
```

Zprávy: `HANDSHAKE_REQ/ACK`, `MONITOR_INFO`, `MOUSE_MOVE`, `MOUSE_BUTTON`, `MOUSE_SCROLL`, `KEY_EVENT`, `CLIPBOARD_PUSH`, `CONTROL_GRANT`, `CONTROL_RELEASE`, `PING/PONG`.

### Zachytávání vstupu (server)

Server instaluje Win32 **low-level hooky** (`SetWindowsHookEx`):
- `WH_MOUSE_LL` — zachytí pohyb myši, kliknutí, scroll
- `WH_KEYBOARD_LL` — zachytí každý stisk a uvolnění klávesy

Hooky běží ve vlastním vlákně s Win32 message pump. Callback jen vloží událost do fronty a okamžitě vrátí (musí být rychlý — Windows hook s timeoutem > ~200 ms odinstaluje). Při aktivním přesměrování hook vrátí `1` místo předání dál, čímž událost potlačí (klávesa/myš "nepropadne" do lokálního systému).

### Detekce přechodu (edge detector)

Uživatel v GUI definuje, ke které hraně kterého monitoru je PC2 přichycen. Z toho se spočítá **virtuální obdélník** v souřadnicovém systému celé plochy PC1. Při každém `WM_MOUSEMOVE` se testuje, zda kurzor vstoupil do tohoto obdélníku:

- Vstup → `CONTROL_GRANT`, kurzor se zamkne na hranici (`SetCursorPos`), kurzor se skryje (`ShowCursor(False)`), souřadnice se přepočítají na souřadnicový prostor klienta a pošlou přes TCP.
- Výstup → `CONTROL_RELEASE`, kurzor se zobrazí.

### Simulace vstupu (klient)

Klient přijímá zprávy a simuluje vstup přes Win32 `SendInput` — nízkoúrovňové API, které injektuje události jako by přišly z hardwaru. Absolutní pozice myši se normalizuje do rozsahu `[0, 65535]` přes celý virtuální desktop klienta (`MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK`).

### Fyzické uchopení myši na PC2

Klient nainstaluje lokální `WH_MOUSE_LL` hook, který potlačuje fyzický pohyb myši (aby neinterferoval s pohybem injektovaným serverem). Pokud fyzický delta pohybu přesáhne práh (30 px), znamená to, že uživatel u PC2 fyzicky pohnul myší — klient pošle `CONTROL_RELEASE_REQUEST` a server mu vrátí kontrolu.

### Synchronizace schránky

Obě strany registrují Win32 `AddClipboardFormatListener` na skrytém okně (message-only HWND). Při změně schránky (`WM_CLIPBOARDUPDATE`) se text přečte a pošle druhé straně jako `CLIPBOARD_PUSH`. Anti-loop flag `_suppress_next` zabrání tomu, aby přijetí způsobilo další odeslání.

---

## Omezení

- Pouze Windows (Win32 API)
- Pouze text ve schránce (obrázky nejsou synchronizovány)
- Hooky nezachytí vstupy v elevated oknech bez spuštění jako správce
- Při vysoké latenci sítě může být pohyb myši trhavý

---

## Firewall

Na PC1 je potřeba povolit port `25432` (TCP příchozí). Při prvním spuštění Windows Firewall pravděpodobně sám zobrazí dialog.

Manuálně:
```
netsh advfirewall firewall add rule name="UniDesk" dir=in action=allow protocol=TCP localport=25432
