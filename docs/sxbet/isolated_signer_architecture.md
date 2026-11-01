# SX Bet: dove custodire la chiave e far girare il signer (GitHub, Render, VPS, KMS)

Mandato `FCB-SXBET-MAGIC-WALLET-FULL-AUDIT-01` §6, §7, §8 e §15. Prezzi e caratteristiche sono quelli delle pagine ufficiali al 01-11-2026: render.com/pricing e render.com/docs, docs.github.com, docs.aws.amazon.com/kms, cloud.google.com/kms, learn.microsoft.com/azure/key-vault, Azure Retail Prices API, hetzner.com/cloud.

**Nessun secret è stato letto, creato o modificato.**

## 1. GitHub Actions Secrets: solo per CI, mai per firmare

| Aspetto | Fatto ufficiale | Conseguenza |
|---|---|---|
| Durata | "6 hours of execution time" per job; "each GitHub-hosted runner is a new virtual machine" | è un job batch, non un servizio sempre attivo, e non riceve richieste in ingresso |
| Chi legge i secret | "Any user with write access to your repository has read access to all secrets" | chiunque abbia accesso in scrittura potrebbe far firmare un workflow |
| Log | "automatic redaction is not guaranteed"; "Structured data can cause secret redaction within logs to fail" | rischio di esposizione nei log |
| Fork | "secrets are not passed to the runner when a workflow is triggered from a forked repository" | protezione utile per la CI, irrilevante per la firma |
| Revisori richiesti | "a job cannot access environment secrets until one of the required reviewers approves it"; sui piani Free, Pro e Team solo per repository pubblici | l'approvazione riguarda l'esecuzione, **non** i parametri esatti dell'ordine; non è la conferma del §2.6 |

**Risposta:** GitHub Actions va usato **solo per CI, build, test e l'audit read-only** (environment `sxbet-live-readonly`). Non va usato per firmare ordini in tempo reale: è vietato anche da governance §2.2 e §2.6.

Un GitHub secret **non** arriva a Render e non va usato per trasferire la chiave.

## 2. Architetture di hosting

| | A: MCP e signer nello stesso servizio Render | B: signer come servizio Render privato separato | C: signer su VPS privata | D: signer con chiave in KMS/HSM |
|---|---|---|---|---|
| Isolamento della chiave | **insufficiente**: la chiave sta nel processo esposto a internet che esegue i tool del modello | buono: processo separato, nessun ingresso pubblico, raggiungibile solo dalla rete privata Render | buono, se la VPS è curata (firewall, aggiornamenti) | **massimo**: la chiave non esce dall'HSM, il servizio chiede solo firme |
| Superficie d'attacco | tutta quella del MCP | rete privata, più l'account Render | SSH e il sistema operativo | il servizio, più le credenziali cloud; la chiave non è estraibile |
| EIP-712 | sì | sì | sì | sì, firmando il digest (vedi §3) |
| Continuità | — | il servizio non va in sleep (piano a pagamento) | dipende dalla VPS | dipende dal servizio che chiama il KMS |
| Latenza per ordine | — | rete interna Render, stessa regione (Frankfurt), pochi ms | internet, decine di ms | più una chiamata KMS, decine di ms |
| Autenticazione tra servizi | — | rete privata più token o mTLS tra i servizi | mTLS o WireGuard | IAM del cloud più mTLS |
| Persistenza del ledger | — | disco persistente $0,25/GB al mese, oppure ancoraggio PostgreSQL (migration 0008) | disco locale | come B o C |
| Rotazione | — | env o secret file, a cura dell'owner | file o secret manager | nuova chiave KMS |
| Costo mensile | — | Starter **$7** + disco 1 GB **$0,25** ≈ **$7,25** | Hetzner da **€5,99** (Cost-Optimized) o **€6,49** (Regular), IVA esclusa; disponibilità non garantita | $1/chiave (AWS) + $0,15 ogni 10.000 firme, più l'hosting di B o C |
| Manutenzione | — | bassa | media o alta | media |
| Stato nel repository | **vietata** (§2.6: il signer non si importa nel MCP né su Render) | **bloccata dal codice attuale**, vedi sotto | compatibile | compatibile, ma serve un adattatore KMS |

Dettagli Render (VERIFIED):
- private service e background worker non hanno un piano gratuito (Starter $7, Standard $25, Pro $85);
- "Free web services can send private network requests, but they can't receive them": il MCP gratuito **può** chiamare un signer privato a pagamento nella stessa regione;
- i secret file sono "plaintext files" in `/etc/secrets/<filename>`, 1 MB in totale;
- il disco persistente esclude lo scaling e i deploy senza downtime.

**Vincolo del codice attuale:** `signer/__init__.py` rifiuta l'import quando esiste la variabile `RENDER`, che Render imposta su **ogni** servizio. L'opzione B richiede quindi:
- una decisione dell'owner;
- una PR di governance e di codice mergiata dall'owner, che distingua il servizio MCP dal servizio signer (per esempio verificando `RENDER_SERVICE_ID` contro un ID consentito) senza indebolire il divieto di importare il signer nel MCP.

## 3. KMS/HSM con la chiave Magic esistente

| | AWS KMS | GCP Cloud KMS | Azure Key Vault |
|---|---|---|---|
| secp256k1 | `ECC_SECG_P256K1` ("commonly used for cryptocurrencies") | `EC_SIGN_SECP256K1_SHA256`, solo livelli HSM | `P-256K` (Standard software o Premium HSM) |
| Import di una chiave esistente | VERIFIED: import per "Asymmetric KMS keys (except ML-DSA and ML-KEM)" | PKCS#8 DER; import secp256k1 `NOT_VERIFIED` | VERIFIED: "may be used to import any key type" |
| Firma | `MessageType=DIGEST`, DER (RFC 3279) | forma "normalized" (low-s) | non verificato |
| Costo per chiave al mese | $1 | ≈ $2,50 (HSM) | $5 HSM; chiave software senza costo fisso visto |
| Costo ogni 10.000 firme | $0,15 | $0,03 | $0,15 |

- Il digest EIP-712 si firma direttamente.
- Il signer deve convertire DER in r e s, forzare low-s (AWS) e calcolare `v` provando la recovery contro l'indirizzo noto (deduzione da specifiche).
- **L'import della chiave Magic in un KMS lo fa l'owner**, una sola volta, dal proprio dispositivo. La chiave non passa mai dal modello, da GitHub o dal MCP.

## 4. Raccomandazione

**Miglior compromesso tra semplicità, affidabilità e isolamento:** **B**, cioè un signer su un servizio Render privato separato.
- Costa circa **$7,25 al mese** e sta nella stessa regione e nello stesso workspace del MCP.
- È stato del ledger in PostgreSQL, oppure ancorato con la migration 0008 su un database o un ruolo dell'owner.
- **Passo di hardening consigliato:** **D sopra B**, con la chiave importata in AWS KMS (circa $1 al mese più costi trascurabili per firma). Così nemmeno un accesso al servizio signer può esportare la chiave, che custodisce **tutti** i fondi del proxy (vedi `magic_wallet_existing_account_audit.md` §3).
- C equivale a B per isolamento e costa poco meno, ma richiede manutenzione di sistema.
- A è esclusa.

## 5. Conferma dell'owner senza aprire un wallet a ogni ordine

Il §2.6 richiede, per ogni ordine reale, una conferma specifica dell'owner su evento, mercato, lato, importo, quota limite ed esposizione massima. Requisiti della conferma:
- autenticata e **fuori banda**: non testo in chat, output di tool o pagine web;
- **verificata dal signer**;
- legata all'hash dei parametri esatti;
- monouso e con scadenza breve.

La risposta testuale di ChatGPT o Claude, o un codice TOTP digitato in chat, **non** soddisfano questi requisiti.

Progetto (compatibile con il codice attuale, binding v2 Ed25519 in `signer/approval.py`):
1. in chat, il modello chiama un tool di **preparazione** (oggi `sxbet_simulate_order`). Il MCP inoltra l'intent al signer, che risponde con l'hash del binding e un link di approvazione a vita breve;
2. sul telefono, l'owner apre una **app di approvazione** (PWA servita dal signer, o app dedicata), che mostra i parametri leggibili del binding: evento, mercato, lato, importo, quota limite, commissioni attese, esposizione massima, scadenza;
3. l'owner approva con un gesto biometrico. La chiave di approvazione (Ed25519, oppure una passkey WebAuthn P-256) vive solo nel dispositivo;
4. il signer verifica firma, binding, nonce e scadenza (≤ 120 s), esegue il risk check, firma l'`Order` con la chiave EOA e lo passa al gateway;
5. il risultato della riconciliazione torna in chat.

Non serve aprire Magic né MetaMask. La firma EIP-712 la fa il signer; l'owner approva con un tap fuori dalla chat.

**Componenti mancanti:**
- l'app di approvazione;
- un verificatore WebAuthn, se si sceglie la passkey invece di Ed25519;
- il canale autenticato MCP → signer;
- il trasporto reale.

**Conflitto da decidere (governance §0):** un "mandato di sessione" (per esempio: N ordini entro i limiti per 30 minuti con una sola approvazione) ridurrebbe i tap, ma **viola il §2.6** ("Un mandato generale non autorizza ordini reali"). Non è progettato. Serve una modifica di governance da parte dell'owner, se mai la volesse.

Il modello **non** ha accesso a una funzione generica di firma: il signer firma solo `Order` sul dominio dell'escrow e rifiuta `SafeTx`, permit, `personal_sign` e messaggi arbitrari.
