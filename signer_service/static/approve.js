"use strict";
(function () {
  const id = location.pathname.split("/")[2];
  const $ = (x) => document.getElementById(x);
  const ROME = { timeZone: "Europe/Rome", dateStyle: "short", timeStyle: "medium" };
  let view = null;

  function row(label, value) {
    const dt = document.createElement("dt"); fcb.text(dt, label);
    const dd = document.createElement("dd"); fcb.text(dd, value);
    $("fields").append(dt, dd);
  }
  function rome(ts) { return new Date(ts * 1000).toLocaleString("it-IT", ROME) + " (Europe/Rome)"; }

  function renderOrder(d) {
    const side = d.selection.semantics === "BACK_OUTCOME_ONE" ? "esito 1" : "esito 2";
    row("Operazione", d.operation_id);
    row("Evento", d.event_label + " · id " + d.event_id);
    row("Mercato", d.market_label + " · " + d.market_hash);
    row("Selezione", "PUNTA su " + d.selection.label + " (" + side + "; isMakerBettingOutcomeOne=" +
        d.selection.is_maker_betting_outcome_one + "). SX Bet non ha BUY/SELL: per 'bancare' un esito si punta l'altro.");
    row("Tipo", d.time_in_force);
    row("Importo (stake)", d.stake_usdc + " USDC (" + d.stake_base_units + " unità base)");
    row("Perdita massima", d.max_potential_loss_usdc + " USDC");
    row("Quota limite (peggiore accettata)", d.decimal_odds_limit + " decimale · " + d.implied_probability_limit_pct +
        "% implicita · percentageOdds " + d.percentage_odds_limit);
    row("Quota vista", d.decimal_odds_quoted + " decimale · " + d.implied_probability_quoted_pct + "% implicita");
    row("Slippage massimo", d.slippage_limit_bps + " bps rispetto alla quota vista (DERIVED)");
    row("Ritorno lordo potenziale", d.potential_payout_before_fees_usdc + " USDC prima delle commissioni (DERIVED)");
    row("Commissioni attese", JSON.stringify(d.expected_fees) + " (applicazione DOCS_UNVERIFIED)");
    row("Esposizione massima", d.max_exposure_base_units + " unità base");
    row("Scadenza ordine", rome(d.order_expiry) + " · " + d.order_expiry_utc);
    row("Rete", d.network + " · chainId " + d.chain_id + " · contratto " + d.verifying_contract);
    row("Maker (chiave effimera DEV)", d.maker);
    row("Fonte dati", d.data_source);
  }
  function renderCancel(d) {
    row("Operazione", d.operation_id);
    row("Effetto", d.effect);
    row("Ambito", d.scope);
    if (d.order_ids && d.order_ids.length) row("Ordini", d.order_ids.join(", "));
    if (d.market_hash) row("Mercato", d.market_hash);
    if (d.event_id) row("Evento", d.event_id);
    row("Rete", d.network);
    row("Fonte dati", d.data_source);
  }

  function tick() {
    if (!view || view.state !== "PENDING_APPROVAL") return;
    const left = view.expires_at - Math.floor(Date.now() / 1000);
    fcb.text($("countdown"), left > 0 ? "Scade tra " + left + " s" : "Scaduta");
    if (left <= 0) { $("approve").disabled = true; $("reject").disabled = true; }
  }

  async function decide(decision) {
    $("approve").disabled = true; $("reject").disabled = true;
    const challenge = decision === "APPROVE" ? view.challenge_approve : view.challenge_reject;
    try {
      const cred = await navigator.credentials.get({ publicKey: {
        challenge: fcb.b64urlToBytes(challenge), rpId: view.rp_id, userVerification: "required", timeout: 60000,
      } });  // discoverable passkey: no credential id list is ever sent to the page
      const r = await fcb.postJSON("/approve/" + id + "/assertion", { decision, ...parts(cred) });
      fcb.text($("result"), r.body.error ? "Rifiutato: " + r.body.error
        : "Stato: " + r.body.state + (r.body.reason ? " (" + r.body.reason + ")" : "") +
          (r.body.gateway ? " · gateway: " + r.body.gateway : "") + " · nessun invio all'exchange");
    } catch (e) {
      fcb.text($("result"), "Operazione annullata o passkey non disponibile");
    }
  }

  function parts(cred) {
    return {
      credential_id: fcb.bytesToB64url(cred.rawId),
      authenticator_data: fcb.bytesToB64url(cred.response.authenticatorData),
      client_data_json: fcb.bytesToB64url(cred.response.clientDataJSON),
      signature: fcb.bytesToB64url(cred.response.signature),
    };
  }

  // Step 1: unlock. The server returns only an opaque challenge; no order detail before this succeeds.
  async function unlock() {
    $("unlock").disabled = true;
    try {
      const o = await fcb.postJSON("/approve/" + id + "/unlock/options", {});
      if (o.body.error) { fcb.text($("state"), "Errore: " + o.body.error); return; }
      const cred = await navigator.credentials.get({ publicKey: {
        challenge: fcb.b64urlToBytes(o.body.challenge), rpId: o.body.rp_id,
        userVerification: o.body.user_verification, timeout: o.body.timeout } });
      const u = await fcb.postJSON("/approve/" + id + "/unlock", { unlock_id: o.body.unlock_id, ...parts(cred) });
      if (u.body.error) { fcb.text($("state"), "Sblocco rifiutato"); $("unlock").disabled = false; return; }
      $("unlock").hidden = true;
      await load();
    } catch (e) {
      fcb.text($("state"), "Sblocco annullato o passkey non disponibile");
      $("unlock").disabled = false;
    }
  }

  // Step 2: details (session cookie, once), then the order-bound decision.
  async function load() {
    const r = await fetch("/approve/" + id + "/challenge", { cache: "no-store", credentials: "same-origin" });
    view = await r.json();
    if (view.error) { fcb.text($("state"), "Errore: " + view.error); return; }
    fcb.text($("state"), (view.kind === "ORDER" ? "Ordine" : "Cancellazione") + " · stato " + view.state);
    if (view.state !== "PENDING_APPROVAL") return;
    (view.kind === "ORDER" ? renderOrder : renderCancel)(view.display);
    fcb.text($("raw"), JSON.stringify({ binding: view.binding, display: view.display }, null, 2));
    $("approve").disabled = false; $("reject").disabled = false;
    $("approve").addEventListener("click", () => decide("APPROVE"));
    $("reject").addEventListener("click", () => decide("REJECT"));
    tick(); setInterval(tick, 1000);
  }
  $("unlock").addEventListener("click", unlock);
})();
