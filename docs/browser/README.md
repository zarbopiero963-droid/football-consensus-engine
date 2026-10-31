# Browser Use e CloakBrowser nel bridge MCP

Tutti i browser sono **di proprietà del bridge**: il client riceve solo un `session_id` opaco e non riceve mai il CDP URL. Il bridge è infrastruttura: restituisce lo stato grezzo del browser e non interpreta le pagine.

## Provider

| Provider | Tool di creazione | Dove gira | Configurazione |
|---|---|---|---|
| Browser Use Cloud (default) | `browser_create(proxy_country_code?)` | cloud Browser Use, via CDP + Playwright | `BROWSER_USE_API_KEY` |
| CloakBrowser (opzionale) | `browser_create_cloak()` | Chromium locale sull'istanza Render; id `cloak-*` | `CLOAKBROWSER_ENABLED=true`. Opzionali: `CLOAKBROWSER_HEADLESS` (default true), `CLOAKBROWSER_HUMANIZE` (default true), `CLOAKBROWSER_PROXY_URL`, `CLOAKBROWSER_MAX_LOCAL_SESSIONS` (default 1, massimo 4) |

`browser_health` riporta se ogni provider è configurato e disponibile e lo stato del database, senza esporre segreti.

## Ciclo di vita

1. **Creazione:** il bridge registra una lease durevole in PostgreSQL prima di restituire la sessione. Se la registrazione fallisce, la sessione viene chiusa.
2. **Uso:** `browser_open_url`, `browser_get_html`, `browser_get_dom`, `browser_get_elements`, `browser_click`, `browser_fill`, `browser_type`, `browser_press`, `browser_select_option`, `browser_set_checked`, `browser_hover`, `browser_focus`, `browser_scroll`, `browser_wait_for_selector`, `browser_wait`, `browser_back`, `browser_forward`, `browser_reload`, `browser_screenshot`, tab (`browser_list_pages`, `browser_new_page`, `browser_switch_page`, `browser_close_page`) e `browser_get_network_responses`.
3. **Chiusura:** `browser_stop(session_id)`, da chiamare sempre a fine lavoro.
4. **Pulizia:**
   - `browser_reap(idle_seconds)` accetta valori 60..21600 e restituisce un errore strutturato fuori da questo intervallo;
   - `browser_stop_all_idle` e `browser_stop_all` (stop d'emergenza) agiscono solo sulle sessioni locali di questa istanza;
   - `browser_list_sessions`, `browser_session_status` e `browser_cleanup_report` servono per ispezionare lo stato.

## Sicurezza

- Si naviga solo verso URL `http`/`https` **pubblici**: indirizzi interni o privati, come `127.0.0.1`, vengono rifiutati (verificato il 31-10-2026).
- Nessun bypass di login, paywall, CAPTCHA o controlli di accesso. Una challenge risolta dal browser stesso viene registrata così com'è.
- `browser_get_elements` omette i valori degli input.
- ⚠️ `browser_create` restituisce oggi `live_url` di Browser Use, che contiene l'host CDP della sessione. La correzione è nella PR #133 e attende una decisione dell'owner.

## Verifica reale (31-10-2026, connettore MCP Claude → produzione)

| Prova | Cloak | Browser Use |
|---|---|---|
| create | PASS | PASS |
| `open_url https://example.com` (200) | PASS | PASS |
| `get_elements` | PASS | — |
| stop | PASS | PASS |
| guard SSRF (`127.0.0.1`) | PASS | — |

Il client ChatGPT va verificato a parte dall'owner (CLIENT_ACTION_REQUIRED).
