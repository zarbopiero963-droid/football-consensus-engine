# SX Bet: compatibilità KMS/HSM con la firma EIP-712 degli ordini OBv3

Mandato owner `SXBET-SECURE-SIGNER-PREPARATION-01` §4. Analisi sulla sola documentazione ufficiale, letta il 01-11-2026.

**Nessun account creato, nessuna chiamata CLI o API verso un cloud, nessun acquisto, nessun servizio configurato. Nessuna chiave reale usata.** I test usano solo chiavi secp256k1 usa e getta generate in memoria.

Etichette: **VERIFIED** = testo citato dalla pagina ufficiale indicata; **NOT_VERIFIED** = non dimostrato dalla fonte ufficiale (dedotto, assente o non leggibile); **TESTED_OFFLINE** = dimostrato dai test locali di questo repository.

## 1. Che cosa deve firmare il KMS

Il signer (`signer/eip712.py`) calcola il digest EIP-712 dell'ordine:

`digest = keccak256(0x19 0x01 || domainSeparator || structHash)`, cioè 32 byte. È anche l'`orderId`.

SX Bet (OpenZeppelin ECDSA lato contratto) si aspetta una firma ECDSA secp256k1 **su quei 32 byte esatti**, impacchettata come `r (32) || s (32) || v (1)`, con `v ∈ {27, 28}` e `s` nella metà bassa (`s ≤ n/2`).

Il digest **non** è un SHA-256. Un KMS che dichiara "ECDSA con SHA-256" è utilizzabile solo se accetta un digest pre-calcolato **senza ri-hasharlo**. ECDSA in sé non dipende dalla funzione di hash: firma l'intero di 256 bit che riceve. Quindi un digest Keccak di 32 byte passato al posto di un SHA-256 produce una firma ECDSA valida su quel valore.

## 2. Matrice di compatibilità

| | AWS KMS | Google Cloud KMS | Azure Key Vault | YubiHSM 2 |
|---|---|---|---|---|
| Curva secp256k1 | VERIFIED `ECC_SECG_P256K1` | VERIFIED `EC_SIGN_SECP256K1_SHA256`, solo livelli `hsm` e `hsm-single-tenant` | VERIFIED `P-256K` (SECP256K1), algoritmo `ES256K` | `eck256` elencato; mappatura su secp256k1 **NOT_VERIFIED** sulla pagina letta |
| Firma di un digest senza ri-hash | VERIFIED: `MessageType=DIGEST` | VERIFIED: campo `digest` | VERIFIED: Sign = "sign hash" | VERIFIED: il comando riceve l'hash `H` |
| Lunghezza del digest | VERIFIED: deve essere quella dell'hash dell'algoritmo (32 byte per `ECDSA_SHA_256`) | VERIFIED: "same digest algorithm as specified by the key version's algorithm" (formato `sha256`, 32 byte) | 32 byte (deduzione da ES256K "for SHA-256 digests") | "possibly zero-left-padded hash" |
| Formato della firma | VERIFIED: DER (ANSI X9.62, RFC 3279 §2.2.3) | DER: VERIFIED solo nel campione C# ("returns signatures in a DER-encoded format") | **NOT_VERIFIED** sulla pagina Azure; per JWA (RFC 7518 §3.4) sarebbe `r‖s` grezzo da 64 byte | VERIFIED: "signature plus its DER encoding" |
| low-s | **NOT_VERIFIED** (non documentato: va normalizzato) | VERIFIED: solo forma "normalized" ("lower-S form") | NOT_VERIFIED | NOT_VERIFIED |
| Recovery id `v` | assente (DER contiene solo r, s) | assente | assente | assente |
| Import di una chiave secp256k1 esistente | VERIFIED: import per "Asymmetric KMS keys (except ML-DSA and ML-KEM keys)", PKCS#8 DER, esempio OpenSSL `ECC_SECG_P256K1_PrivateKey.der` | PKCS#8 DER VERIFIED per EC; per secp256k1 **NOT_VERIFIED** (la pagina mostra solo P-256) | BYOK EC-HSM VERIFIED con esempio solo `P-256`; per `P-256K` **NOT_VERIFIED**; serve uno strumento BYOK di un HSM di vendor | VERIFIED: `PUT ASYMMETRIC KEY` ("Import an Asymmetric Key into the device") |
| Costo chiave/mese | VERIFIED $1 | VERIFIED HSM EC $0,003424658/ora (≈ $2,50/mese) fino a 2.000 versioni | importi **NOT_VERIFIED** (la pagina li carica via JavaScript); struttura VERIFIED: chiavi ECC HSM solo Premium, per chiave al mese + per 10.000 transazioni | hardware da acquistare, prezzo non verificato |
| Costo firme | VERIFIED solo come esempio: "100,000 requests at $0.15 per 10,000 requests" (chiave ECC); tariffa base "asymmetric requests except RSA 2048" dipende dalla regione, importo NOT_VERIFIED | VERIFIED "$0.03 / 10,000" nell'esempio "asymmetric signing"; la colonna esatta della tabella HSM è ambigua nel testo estratto | NOT_VERIFIED | nessuno |
| Esito per SX Bet | **compatibile** con adattatore (DER → r,s,v; low-s) | **compatibile** con adattatore (DER → r,s,v); import secp256k1 da confermare | **probabile**, formato di output da confermare prima dell'uso | **probabile**, da confermare la curva |

Ledger e altri hardware wallet non sono stati valutati: firmano EIP-712 con conferma sul dispositivo, che è una UX diversa dal signer automatico. Le loro capacità non sono state verificate sulla documentazione ufficiale e restano **NOT_VERIFIED**.

## 3. Citazioni

### AWS KMS

[API Sign](https://docs.aws.amazon.com/kms/latest/APIReference/API_Sign.html):
- "If you provide a message digest, use the `DIGEST` value of `MessageType` to prevent the digest from being hashed again while signing."
- "When the value of `MessageType` is `RAW`, AWS KMS uses the standard signing algorithm, which begins with a hash function. When the value is `DIGEST`, AWS KMS skips the hashing step in the signing algorithm."
- "When the value of `MessageType` is `DIGEST`, the length of the `Message` value must match the length of hashed messages for the specified signing algorithm."
- "You can submit a message digest and omit the `MessageType` or specify `RAW` so the digest is hashed again while signing. However, this can cause verification failures when verifying with a system that assumes a single hash."
- Signature: "When used with the `ECDSA_SHA_256`, `ECDSA_SHA_384`, or `ECDSA_SHA_512` signing algorithms, this value is a DER-encoded object as defined by ANSI X9.62–2005 and RFC 3279 Section 2.2.3."
- Il default di `MessageType` è `RAW` ("Required: No"): va **sempre** impostato in modo esplicito.

[Key spec reference](https://docs.aws.amazon.com/kms/latest/developerguide/symm-asymm-choose-key-spec.html):
- "ECC_SECG_P256K1 (secp256k1), commonly used for cryptocurrencies."
- Tabella: `ECC_SECG_P256K1` → `ECDSA_SHA_256`, "NIST FIPS 186-4, Section 6.4, ECDSA signature using the curve specified by the key and SHA-256 for the message digest."
- Nessuna menzione di low-s o di una firma canonica.

[Importing key material](https://docs.aws.amazon.com/kms/latest/developerguide/importing-keys.html):
- "AWS KMS supports imported key material for the following types of KMS keys. [...] Asymmetric KMS keys (except ML-DSA and ML-KEM keys)".
- "You cannot import key material into KMS keys in custom key stores."

[Step 3: Encrypt the key material](https://docs.aws.amazon.com/kms/latest/developerguide/importing-keys-encrypt-key-material.html):
- "For asymmetric key pairs, encrypt and import only the private key. AWS KMS derives the public key from the private key."
- Formato PKCS#8 DER (`openssl pkcs8 -topk8 -outform der -nocrypt`), con l'esempio `ec_paramgen_curve:secp256k1 ... > ECC_SECG_P256K1_PrivateKey.der`.
- Wrapping: `RSAES_OAEP_SHA_1` e `RSAES_OAEP_SHA_256` "for [...] elliptic curve (ECC) private keys", oppure `RSA_AES_KEY_WRAP_SHA_1` e `RSA_AES_KEY_WRAP_SHA_256`.

[Pricing](https://aws.amazon.com/kms/pricing/):
- "Each AWS KMS key that you create in AWS KMS costs $1/month (prorated hourly)."
- Le richieste Sign su chiavi asimmetriche sono escluse dal free tier ("requests to API operations such as Sign, Verify, Encrypt, Decrypt, and GetPublicKey that reference asymmetric KMS keys").
- Esempio "File signing application": "1 ECC 256 KMS key [...] 100,000 requests at $0.15 per 10,000 requests Total: $2.50/month".

### Google Cloud KMS

[Algorithms](https://docs.cloud.google.com/kms/docs/algorithms):
- `EC_SIGN_SECP256K1_SHA256`: "ECDSA on the Secp256k1 Curve with a SHA-256 digest."
- È elencato sotto `hsm` e `hsm-single-tenant`, ma non sotto `software`, `external` o `external-vpc`.
- "secp256k1 curves generate signatures in the "normalized" form only (also known as the "lower-S form")."

[asymmetricSign REST](https://docs.cloud.google.com/kms/docs/reference/rest/v1/projects.locations.keyRings.cryptoKeys.cryptoKeyVersions/asymmetricSign):
- `digest`: "The digest must be produced with the same digest algorithm as specified by the key version's algorithm."
- `data`: "The data to sign. It can't be supplied if AsymmetricSignRequest.digest is supplied."

[Create and validate signatures](https://docs.cloud.google.com/kms/docs/create-validate-signatures): "Cloud KMS returns signatures in a DER-encoded format" (campione C#).

[Importing a key](https://docs.cloud.google.com/kms/docs/importing-a-key):
- La chiave va fornita "in PKCS #8 DER as required by Google Cloud KMS".
- Metodi di wrapping: `rsa-oaep-3072-sha1-aes-256`, `rsa-oaep-4096-sha1-aes-256`, `rsa-oaep-3072-sha256-aes-256`, `rsa-oaep-4096-sha256-aes-256`, `rsa-oaep-3072-sha256`, `rsa-oaep-4096-sha256`.
- L'import di `EC_SIGN_SECP256K1_SHA256` non è menzionato: **NOT_VERIFIED**.

[Pricing](https://cloud.google.com/kms/pricing): "Active asymmetric RSA 3072, RSA 4096 or Elliptic Curve key versions": `$0.003424658 / 1 hour` (HSM, da 0 a 2.000 versioni). Esempio: "50,000 key use operations for asymmetric signing at $0.03 per 10,000 operations".

### Azure Key Vault

[Key types, algorithms, and operations](https://learn.microsoft.com/en-us/azure/key-vault/keys/about-keys-details):
- "P-256K - The SEC curve SECP256K1".
- "ES256K - ECDSA for SHA-256 digests and keys created with curve P-256K. This algorithm is pending standardization."
- "Strictly, this operation is "sign hash" or "verify hash", as the service doesn't hash content as part of signature creation."
- "Import: [...] Asymmetric keys can be imported by using several different packaging methods within a JWK construct."

[REST sign](https://learn.microsoft.com/en-us/rest/api/keyvault/keys/sign/sign):
- "Creates a signature from a digest using the specified key."
- `ES256K`: "ECDSA using P-256K and SHA-256, as described in https://tools.ietf.org/html/rfc7518".
- Il formato di `value` nella risposta non è descritto oltre "The result of the operation": **NOT_VERIFIED**.

[BYOK specification](https://learn.microsoft.com/en-us/azure/key-vault/keys/byok-specification):
- "For an EC key, the private key uses ASN.1 DER encoding [RFC5915] wrapped in PKCS#8 [RFC5208]."
- L'import richiede il "HSM vendor provided BYOK tool".
- L'esempio usa solo `--curve-name "P-256"`.

[Pricing](https://azure.microsoft.com/en-us/pricing/details/key-vault/): "HSM-protected keys [...] Elliptic-Curve Cryptography (ECC) keys N/A [Standard] [...] $- per key per month". Gli importi sono caricati dinamicamente e non risultano nel testo: **NOT_VERIFIED** (il valore "$5 HSM" di `isolated_signer_architecture.md` §3 non è stato riverificato qui).

### YubiHSM 2

[Command reference](https://docs.yubico.com/hardware/yubihsm-2/hsm-2-user-guide/hsm2-cmd-reference.html):
- SIGN ECDSA: "The DSI for ECDSA is a possibly zero-left-padded hash of the data, H". In risposta: "The length of DS [...] equals the length of the signature plus its DER encoding."
- PUT ASYMMETRIC KEY: "Import an Asymmetric Key into the device."
- Algoritmi: "... ecp521, eck256, ecbp256 ...". La pagina non dice che `eck256` sia secp256k1.

## 4. Le trappole, una per una (TESTED_OFFLINE)

Coperte da `tests/test_sxbet_kms_adapter.py`, con un "KMS" emulato da una chiave secp256k1 usa e getta di `cryptography`, in modalità `ECDSA(Prehashed(SHA256))`.

1. **RAW invece di DIGEST.** Con `MessageType=RAW` (il default AWS) o con il campo `data` di GCP, il KMS firma `sha256(digest)`. La firma è ECDSA valida ma su un altro messaggio, e la recovery sul digest EIP-712 restituisce un indirizzo diverso. L'adattatore lo rifiuta (`KMS_SIGNATURE_DOES_NOT_RECOVER_SIGNER`). Il test dimostra anche che la stessa firma recupera il maker su `sha256(digest)`, cioè che la causa è il doppio hash.
2. **DER.** AWS e GCP restituiscono `SEQUENCE { INTEGER r, INTEGER s }`. Il parser è rigoroso:
   - solo lunghezze in forma breve;
   - interi minimi, non negativi, al massimo 33 byte;
   - nessun byte in coda;
   - `1 ≤ r, s < n`;
   - lunghezza totale ≤ 72 byte.
3. **low-s.** AWS non documenta la forma low-s. Se `s > n/2`, l'adattatore usa `n − s`. Questo inverte la parità di recovery, che viene ricalcolata.
4. **`v` assente.** Il DER non contiene il recovery id. L'adattatore prova `v = 27` e `v = 28` con ecrecover contro l'indirizzo noto e accetta solo se **esattamente una** delle due recupera quell'indirizzo.
5. **Indirizzo dalla chiave pubblica.** `address_from_spki_der()` deriva l'indirizzo EIP-55 dalla SubjectPublicKeyInfo DER (il formato di AWS `GetPublicKey`) e rifiuta qualunque curva diversa da secp256k1.
6. **Digest.** Deve essere esattamente 32 byte `bytes`. Un input diverso viene rifiutato prima di arrivare al backend.
7. **Errori.** Un'eccezione del backend (che potrebbe contenere un ARN, un endpoint o una credenziale) è sostituita dal codice fisso `KMS_BACKEND_FAILED`, senza catena di eccezioni.

La firma convertita viene verificata con l'ecrecover puro-Python e l'encoder EIP-712 manuale di `tests/test_sxbet_eip712_official_vectors.py` (implementazione indipendente), sui domini mainnet (4162) e Toronto (79479957), e anche con `eth_account`.

## 5. Il modulo `signer/kms.py`

- `DigestSigner` (Protocol): `sign_digest(digest: bytes) -> bytes` (DER). Un backend reale lo implementa.
- `parse_der_signature`, `normalize_low_s`, `der_to_ethereum_signature(digest, der, expected_address) -> 65 byte`.
- `address_from_spki_der`.
- `KmsOrderSigner(backend, address)`: `sign_digest()` e `sign_digest_hex()`.

Il modulo non contiene codice di rete né SDK cloud (niente `boto3`, `google-cloud-kms` o `azure-keyvault`). La guardia AST di `tests/test_sxbet_render_isolation.py` resta invariata e lo copre.

## 6. Raccomandazione

1. **AWS KMS `ECC_SECG_P256K1`** è l'opzione meglio documentata per questo caso:
   - firma del digest senza ri-hash, con `MessageType=DIGEST`, VERIFIED;
   - output DER, VERIFIED;
   - import di una chiave asimmetrica esistente in PKCS#8 DER, VERIFIED con un esempio esplicito secp256k1;
   - costo di circa $1 al mese più circa $0,15 ogni 10.000 firme (cifra dell'esempio ufficiale).

   Requisiti obbligatori: `MessageType=DIGEST` sempre esplicito, normalizzazione low-s, calcolo di `v` per recovery. Sono già implementati e testati offline nell'adattatore.
2. **Google Cloud KMS** è un'alternativa valida per una **chiave nuova**: la forma low-s è garantita. L'import di una chiave secp256k1 esistente resta NOT_VERIFIED.
3. **Azure Key Vault** va considerato solo dopo aver verificato il formato di output di `ES256K` (DER o `r‖s`) e l'import BYOK di `P-256K`.
4. Prima di qualunque uso reale serve un test con un account owner e una **chiave nuova, mai finanziata**, che confermi offline che la firma DIGEST recupera l'indirizzo atteso. Va registrato anche se l'output AWS contiene effettivamente `s` alti.

**Usare la chiave Magic reale in un KMS richiede un mandato owner separato.** L'import lo fa l'owner dal proprio dispositivo. La chiave non passa mai dal modello, da GitHub, dal MCP o da questo repository (governance §2.2 e §2.6). Creare account, chiavi o servizi a pagamento è un nuovo costo, quindi è una decisione dell'owner (§2.4).

## 7. Prossimo passo (non fatto in questa PR)

`signer/core.py` resta invariato: l'unico percorso di firma è la chiave in-process da `SXBET_SIGNER_PRIVATE_KEY`.

Per collegare il KMS serve una PR separata, con merge riservato all'owner (§2.6: codice di firma). La PR dovrebbe:
- passare a `Signer` un `KmsOrderSigner` opzionale, mutuamente esclusivo con la chiave env;
- derivare `self.address` da `GetPublicKey` tramite `address_from_spki_der` e confrontarlo con quello configurato dall'owner;
- in `_produce_signature`, firmare `bytes.fromhex(order_id[2:])` e verificare la recovery prima di registrare `SIGNED`;
- implementare un backend concreto (client AWS) fuori da `signer/`, oppure con una guardia di rete aggiornata in modo esplicito, mai nel runtime MCP;
- passare ogni chiamata KMS dal coordinatore di quota se diventa un provider a costo (§2.4).

`LIVE_TRADING` resta `UNAVAILABLE`.
