"use strict";
(function () {
  const $ = (x) => document.getElementById(x);
  $("enroll").addEventListener("click", async () => {
    try {
      const o = await fcb.postJSON("/enroll/options", { enrollment_code: $("code").value.trim() });
      if (o.body.error) { fcb.text($("result"), "Rifiutato: " + o.body.error); return; }
      const opt = o.body;
      const cred = await navigator.credentials.create({ publicKey: {
        challenge: fcb.b64urlToBytes(opt.challenge), rp: opt.rp,
        user: { id: fcb.b64urlToBytes(opt.user.id), name: opt.user.name, displayName: opt.user.displayName },
        pubKeyCredParams: opt.pubKeyCredParams, authenticatorSelection: opt.authenticatorSelection,
        attestation: opt.attestation, timeout: opt.timeout,
      } });  // no excludeCredentials: the server never lists credential ids
      const r = await fcb.postJSON("/enroll/finish", {
        challenge_id: opt.challenge_id,
        attestation_object: fcb.bytesToB64url(cred.response.attestationObject),
        client_data_json: fcb.bytesToB64url(cred.response.clientDataJSON),
      });
      fcb.text($("result"), r.body.error ? "Rifiutato: " + r.body.error : "Passkey registrata (" + r.body.credential_prefix + "…)");
    } catch (e) {
      fcb.text($("result"), "Registrazione annullata o non supportata");
    }
  });
})();
