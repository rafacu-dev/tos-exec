"""
Scripts JavaScript que se inyectan en el webview de thinkorswim.

Portados del proyecto TC-Ai (scaner/public/app.js), probados contra el DOM
real de ToS (selectores data-testid, shadow DOM). Cada builder devuelve un
IIFE async que resuelve a un objeto {ok, state, detail, ...}.
"""
import json

# Helpers JS comunes a varios scripts (visibilidad, shadow DOM, click, etc.)
_COMMON = r"""
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const visible = (el) => {
  if (!el) return false;
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return rect.width > 1 && rect.height > 1 && style.display !== "none"
    && style.visibility !== "hidden" && style.opacity !== "0";
};
const labelOf = (el) => [
  el?.getAttribute?.("aria-label"), el?.getAttribute?.("data-testid"),
  el?.getAttribute?.("title"), el?.getAttribute?.("placeholder"),
  el?.id, el?.innerText, el?.textContent
].filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
const deepQueryAll = (selector, root = document) => {
  const results = [];
  const visit = (node) => {
    try { if (node?.querySelectorAll) results.push(...node.querySelectorAll(selector)); } catch {}
    let children = [];
    try { children = node?.querySelectorAll ? [...node.querySelectorAll("*")] : []; } catch {}
    for (const child of children) { if (child.shadowRoot) visit(child.shadowRoot); }
  };
  visit(root);
  return [...new Set(results)];
};
const setValue = (input, value) => {
  if (!input) return false;
  input.focus();
  const proto = Object.getPrototypeOf(input);
  const desc = Object.getOwnPropertyDescriptor(proto, "value");
  if (desc?.set) desc.set.call(input, ""); else input.value = "";
  input.dispatchEvent(new Event("input", { bubbles: true }));
  if (desc?.set) desc.set.call(input, String(value)); else input.value = String(value);
  input.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: String(value) }));
  input.dispatchEvent(new Event("change", { bubbles: true }));
  return true;
};
const click = (el, label) => {
  if (!el) return false;
  el.scrollIntoView({ block: "center", inline: "center" });
  el.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
  try { el.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, pointerType: "mouse" })); } catch {}
  el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
  el.click();
  el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
  try { el.dispatchEvent(new PointerEvent("pointerup", { bubbles: true, pointerType: "mouse" })); } catch {}
  steps.push(label || labelOf(el).slice(0, 80));
  return true;
};
const pressEnter = (el) => {
  el?.dispatchEvent?.(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
  el?.dispatchEvent?.(new KeyboardEvent("keyup", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true }));
};
const pageText = () => document.body?.innerText?.replace(/\s+/g, " ").trim() || "";
const waitUntil = async (finder, timeoutMs = 12000, stepMs = 300) => {
  const started = Date.now();
  let v = finder();
  while (!v && Date.now() - started < timeoutMs) { await sleep(stepMs); v = finder(); }
  return v;
};
"""


def visible_state_script() -> str:
    """Lee el estado visible: paperOk, liveRisk, loginForm, etc."""
    return r"""
(() => {
  const visible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
  };
  const deepQueryAll = (selector, root = document) => {
    const results = [];
    const visit = (node) => {
      try { if (node?.querySelectorAll) results.push(...node.querySelectorAll(selector)); } catch {}
      let children = [];
      try { children = node?.querySelectorAll ? [...node.querySelectorAll("*")] : []; } catch {}
      for (const child of children) { if (child.shadowRoot) visit(child.shadowRoot); }
    };
    visit(root);
    return [...new Set(results)];
  };
  const text = (document.body?.innerText || "").slice(0, 6000);
  const ts = (() => {
    const paperInput = deepQueryAll("input[data-testid='toggle-right'][aria-label='Paper Money'], input[aria-label='Paper Money'], input[value='Paper Money']")[0] || null;
    const liveInput = deepQueryAll("input[data-testid='toggle-left'][aria-label='Real Live Trading'], input[aria-label='Real Live Trading'], input[value='Real Live Trading']")[0] || null;
    const toggle = paperInput?.closest?.("[data-testid='toggle'], .trading-system-toggle") || liveInput?.closest?.("[data-testid='toggle'], .trading-system-toggle") || null;
    return { hasToggle: Boolean(toggle || paperInput || liveInput), paperChecked: Boolean(paperInput?.checked), liveChecked: Boolean(liveInput?.checked) };
  })();
  const paperText = /paper\s*money|papermoney|paper trading|simulated|simulation/i.test(text);
  const liveText = /live\s*(trading|account)|real\s*money|cash account|margin account/i.test(text);
  const paperOk = ts.hasToggle ? ts.paperChecked : paperText;
  const liveRisk = ts.hasToggle ? (ts.liveChecked && !ts.paperChecked) : (liveText && !paperText);
  const frames = [...document.querySelectorAll("iframe, frame")].map((f) => {
    const r = f.getBoundingClientRect();
    return { src: f.src || "", width: Math.round(r.width), height: Math.round(r.height) };
  }).slice(0, 20);
  return {
    url: location.href, title: document.title, paperOk, liveRisk, tradingSystem: ts,
    loginForm: /log\s*in|sign\s*in|username|password/i.test(text.slice(0, 2500)),
    optionChainVisible: /option chain|strike|expiration|calls|puts/i.test(text),
    tradeVisible: /positions|orders|trade|buy|sell/i.test(text),
    deepInputs: deepQueryAll("input").length,
    frames,
    textSample: text.slice(0, 600)
  };
})();
"""


def paper_mode_script() -> str:
    """Cambia la cuenta a Paper Money si no lo está ya."""
    return r"""
(async () => {
  const steps = [];
""" + _COMMON + r"""
  const getTradingSystem = () => {
    const paperInput = deepQueryAll("input[data-testid='toggle-right'][aria-label='Paper Money'], input[aria-label='Paper Money'], input[value='Paper Money']")[0] || null;
    const liveInput = deepQueryAll("input[data-testid='toggle-left'][aria-label='Real Live Trading'], input[aria-label='Real Live Trading'], input[value='Real Live Trading']")[0] || null;
    const paperLabel = paperInput?.id ? document.querySelector("label[for='" + CSS.escape(paperInput.id) + "']") : null;
    const toggle = paperInput?.closest?.("[data-testid='toggle'], .trading-system-toggle") || liveInput?.closest?.("[data-testid='toggle'], .trading-system-toggle") || null;
    return { paperInput, liveInput, paperLabel, toggle, hasToggle: Boolean(toggle || paperInput || liveInput), paperChecked: Boolean(paperInput?.checked), liveChecked: Boolean(liveInput?.checked) };
  };
  let ts = getTradingSystem();
  if (ts.paperChecked) return { ok: true, state: "PAPER_CONFIRMED", detail: "Paper Money ya activo.", steps };
  if (ts.hasToggle) {
    const target = ts.paperLabel || ts.paperInput || ts.toggle;
    if (target) { click(target, "Seleccionar Paper Money"); try { ts.paperInput?.click?.(); } catch {} await sleep(900); }
    ts = getTradingSystem();
    return { ok: ts.paperChecked, state: ts.paperChecked ? "PAPER_CONFIRMED" : "PAPER_SWITCH_REQUESTED", detail: ts.paperChecked ? "Paper Money activado." : "Cambio solicitado; revalida.", steps };
  }
  const text = (document.body?.innerText || "").slice(0, 9000);
  const paperOk = /paper\s*money|papermoney|paper trading|simulated|simulation/i.test(text) && !/real\s*live\s*trading/i.test(text);
  if (paperOk) return { ok: true, state: "PAPER_CONFIRMED", detail: "Paper Money visible.", steps };
  const liveRisk = /live\s*(trading|account)|real\s*money/i.test(text) && !paperOk;
  return { ok: false, state: liveRisk ? "LIVE_RISK" : "PAPER_NOT_FOUND", detail: liveRisk ? "Cuenta Live; no pude cambiar a Paper." : "No encontré selector Paper Money.", steps };
})();
"""


def login_script(username: str, password: str) -> str:
    """Rellena Login ID + Password y envía (maneja flujo de 2 pasos y MFA).
    Portado de TC-Ai buildThinkOrSwimLoginScript."""
    u = json.dumps(username or "")
    p = json.dumps(password or "")
    return r"""
(async () => {
  const username = """ + u + r""";
  const password = """ + p + r""";
  const steps = [];
""" + _COMMON + r"""
  const text0 = pageText();
  const loggedIn = /positions|orders|trade|option chain|paper\s*money|account/i.test(text0)
    && !/log\s*in|sign\s*in|username|password/i.test(text0.slice(0, 2500));
  if (loggedIn) return { ok: true, state: "ALREADY_LOGGED_IN", detail: "ToS ya parece dentro.", steps };
  if (/multi-factor|mfa|verification code|security code|authenticator|one-time/i.test(text0))
    return { ok: false, state: "MFA_REQUIRED", detail: "Se requiere MFA/código manual.", steps };

  const allInputs = () => deepQueryAll("input").filter(visible);
  const findUser = () => {
    const exact = deepQueryAll("#loginIdInput, input[aria-label='Login ID'], input[placeholder='Login ID']")[0];
    if (visible(exact)) return exact;
    const ins = allInputs();
    return ins.find((i) => /login\s*id|user|login|email|id|screen|username/i.test(labelOf(i)) && i.type !== "password")
      || ins.find((i) => ["text","email","tel",""].includes(i.type || ""));
  };
  const findPass = () => {
    const exact = deepQueryAll("#passwordInput, input[aria-label='Password']")[0];
    if (visible(exact)) return exact;
    return allInputs().find((i) => i.type === "password" || /password|contrase/i.test(labelOf(i)));
  };
  const findAction = (pat) => deepQueryAll("button, input[type='submit'], input[type='button'], [role='button'], sdps-button, .sdps-button, a")
    .filter(visible).find((b) => pat.test(labelOf(b)) && !/cancel|back|show password|eye|forgot|remember/i.test(labelOf(b)));
  const waitForPassOrMfa = async (ms = 9000) => {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
      const pw = findPass(); if (pw) return { pw };
      if (/multi-factor|mfa|verification code|security code|authenticator|one-time/i.test(pageText())) return { mfa: true };
      await sleep(300);
    }
    return { pw: null };
  };

  const first = await waitUntil(() => findPass() || findUser(), 9000, 250);
  const userInput = first?.type === "password" ? null : (first || findUser());
  if (userInput) { setValue(userInput, username); steps.push("Login ID escrito"); await sleep(550); }
  let passInput = findPass();
  if (userInput && !passInput) {
    const exactCont = deepQueryAll("#continueBtn, button[aria-label='Continue']")[0];
    const next = visible(exactCont) ? exactCont : findAction(/continue|next|submit|sign\s*in|log\s*in|login|log on|sign on/i);
    if (next) click(next, "Continuar Login ID");
    else { pressEnter(userInput); steps.push("Enter Login ID"); }
    const w = await waitForPassOrMfa(9000);
    if (w.mfa) return { ok: false, state: "MFA_REQUIRED", detail: "Se requiere MFA/código manual.", steps };
    passInput = w.pw || findPass();
  }
  if (!passInput) {
    if (/multi-factor|mfa|verification code|security code|authenticator|one-time/i.test(pageText()))
      return { ok: false, state: "MFA_REQUIRED", detail: "Se requiere MFA/código manual.", steps };
    return { ok: false, state: "PASSWORD_FIELD_NOT_FOUND", detail: "No encontré el campo de contraseña.", steps };
  }
  setValue(passInput, password); steps.push("Contraseña escrita"); await sleep(350);
  const submit = findAction(/sign\s*in|log\s*in|login|submit|continue/i);
  if (submit) click(submit, "Enviar login");
  else { pressEnter(passInput); steps.push("Enter contraseña"); }
  return { ok: true, state: "LOGIN_SUBMITTED", detail: "Login enviado. Si aparece MFA, complétalo a mano.", steps };
})();
"""


def option_order_script(symbol: str, side: str, strike: float,
                        expiration: str, quantity: int,
                        max_investment: float, submit: bool,
                        paper_confirmed: bool = False,
                        auto_size: bool = True,
                        max_contracts: int = 1,
                        action: str = "BUY") -> str:
    """
    Busca el símbolo, abre la option chain, selecciona el contrato
    (Call/Put en el strike/vencimiento dados) por Ask para BUY o Bid para SELL,
    fija cantidad y
    MARKET, y -si submit y la cuenta es Paper- envía Review→Send.

    paper_confirmed: True cuando la automatización ya verificó Paper Money por
    el toggle de la cuenta (vía login). Relaja la detección de texto (que falla
    en la página de Trade) pero el bloqueo por señales Live explícitas sigue.
    """
    payload = json.dumps({
        "symbol": symbol,
        "action": "SELL" if action.upper() == "SELL" else "BUY",
        "contract": {"type": "put" if side == "PUT" else "call",
                     "strike": strike, "expiration": expiration},
        "orderPlan": {"quantity": quantity, "maxInvestment": max_investment,
                      "autoSize": bool(auto_size), "maxContracts": int(max_contracts)},
        "submitOrder": bool(submit),
        "paperConfirmed": bool(paper_confirmed),
    })
    return r"""
(async () => {
  const payload = """ + payload + r""";
  const steps = [];
""" + _COMMON + r"""
  const headerText = () => deepQueryAll("img, svg, [class*='logo'], [class*='header'], [data-testid*='account'], [data-testid*='header']")
    .map((e) => [e.getAttribute && e.getAttribute("alt"), e.getAttribute && e.getAttribute("aria-label"), e.textContent].filter(Boolean).join(" ")).join(" ");
  const accountText = () => [pageText(), headerText(), document.title, location.href].join(" ");
  const isPaperMoney = () => /paper\s*money|papermoney|paper trading|simulated|simulation/i.test(accountText());
  const hasLiveRisk = () => /real\s*(money|live)|live\s*trading|live\s*account/i.test(accountText()) && !isPaperMoney();
  const normalizeStrike = (v) => { const n = Number(v); return Number.isFinite(n) ? String(Number(n.toFixed(4))).replace(/\.0+$/, "") : String(v || ""); };
  const monthNames = [["Jan","January"],["Feb","February"],["Mar","March"],["Apr","April"],["May","May"],["Jun","June"],["Jul","July"],["Aug","August"],["Sep","September"],["Oct","October"],["Nov","November"],["Dec","December"]];
  const expirationNeedles = (iso) => { const m = String(iso||"").match(/^(\d{4})-(\d{2})-(\d{2})$/); if (!m) return []; const [,y,mo,d]=m; const dn=String(Number(d)); const [sh,lo]=monthNames[Number(mo)-1]||[]; return [iso,[lo,dn,y].join(" "),[sh,dn,y].join(" "),[lo,dn].join(" "),[sh,dn].join(" ")].filter(Boolean); };

  if (/log\s*in|sign\s*in|username|password/i.test(pageText().slice(0, 2500)))
    return { ok: false, state: "LOGIN_REQUIRED", detail: "ToS muestra login.", steps };

  // 1) buscar símbolo SOLO si no está ya cargado.
  // La app navega por URL (trade?symbol=X) antes de correr este script, así que
  // normalmente el símbolo ya está activo. Buscar de nuevo sobre una página de
  // Trade correcta la saca del contexto del ticket y el botón Review nunca aparece
  // (causa del bucle REVIEW_BUTTON_NOT_FOUND). Solo usamos el campo exacto de Trade;
  // el fallback genérico agarraba inputs de quote/búsqueda equivocados.
  const symU = String(payload.symbol || "").toUpperCase();
  const symbolAlreadyLoaded = () => {
    if (new RegExp("[?&]symbol=" + symU + "(?:[&#]|$)", "i").test(String(location.href || ""))) return true;
    return deepQueryAll("[data-testid*='symbol'], [class*='symbol-detail'], [class*='symbolDetail'], h1, h2")
      .filter(visible).some((e) => new RegExp("(^|[^A-Z])" + symU + "([^A-Z]|$)").test(String(e.textContent || "").toUpperCase()));
  };
  const findSearch = () => {
    const exact = deepQueryAll("#navigation-symbol-search, input[aria-label='Find a symbol'], [data-testid='navigation-symbol-search-form'] input")[0];
    return visible(exact) ? exact : null;
  };
  if (!symbolAlreadyLoaded()) {
    const search = findSearch();
    if (search) { setValue(search, payload.symbol); pressEnter(search); steps.push("Buscar " + payload.symbol); await sleep(1800); }
  }

  // 2) abrir option chain
  const findChainRoot = () => deepQueryAll("section.option-chain.trade-component, section.option-chain, .option-chain").find(visible) || null;
  let chainRoot = await waitUntil(findChainRoot, 8000);
  const findChainExpander = () => deepQueryAll("button[data-testid='expander-btn'], button[aria-label*='Option Chain'], [role='button'][aria-label*='Option Chain'], button[aria-label*='option chain']").find((b) => visible(b) && /option\s*chain/i.test(labelOf(b)));
  const expander = await waitUntil(findChainExpander, 12000);
  if (expander && expander.getAttribute("aria-expanded") !== "true") { click(expander, "Abrir Option Chain"); await sleep(1600); chainRoot = findChainRoot() || chainRoot; }

  // 3) centrar en strike
  const centeredInput = deepQueryAll("input[data-testid='option-chain-centered-on-input'], #centered-on-input", chainRoot || document).find(visible);
  if (centeredInput) { setValue(centeredInput, Number(payload.contract.strike).toFixed(2)); pressEnter(centeredInput); steps.push("Centrar strike " + payload.contract.strike); await sleep(700); }

  // 4) abrir vencimiento
  const findExpirationButtons = () => deepQueryAll("[data-testid='expiration-row-expand-button'], button[data-testid*='expiration'], button[aria-label^='Show ']", chainRoot || document).filter(visible).filter((b) => /show\s+.+options|expiration|weekly|\(\d+\s+days?\)/i.test(labelOf(b)));
  const expButtons = await waitUntil(() => { const b = findExpirationButtons(); return b.length ? b : null; }, 18000);
  const needles = expirationNeedles(payload.contract.expiration);
  const targetExp = (expButtons || []).find((b) => needles.some((n) => labelOf(b).includes(n))) || (expButtons || [])[0];
  if (targetExp && targetExp.getAttribute("aria-expanded") !== "true") { click(targetExp, "Abrir vencimiento"); await sleep(1200); }

	  // 5) seleccionar contrato por Ask/Bid
	  const optionButtons = () => deepQueryAll("[data-testid*='option'], button[aria-label*='Call'], button[aria-label*='Put'], [role='button'][aria-label*='Call'], [role='button'][aria-label*='Put']", chainRoot || document).filter(visible).filter((b) => /\b(Call|Put)\b/i.test(labelOf(b)) && /\b(Bid|Ask|Strike)\b/i.test(labelOf(b)));
  const findOptionButton = (quote) => {
    const side = payload.contract.type === "put" ? "Put" : "Call";
    const strike = normalizeStrike(payload.contract.strike);
    const strikePattern = new RegExp("Strike\\s+" + strike + "(?:\\.0+)?\\b");
    return optionButtons().find((b) => { const l = labelOf(b); return strikePattern.test(l) && new RegExp("\\b"+side+"\\b","i").test(l) && new RegExp("\\b"+quote+"\\b","i").test(l) && !/Size\b/i.test(l); }) || null;
  };
	  const quoteSide = payload.action === "SELL" ? "Bid" : "Ask";
	  const quoteButton = await waitUntil(() => findOptionButton(quoteSide), 12000);
	  if (!quoteButton) return { ok: false, state: "CONTRACT_NOT_FOUND", detail: "No encontré el contrato " + payload.contract.type + " strike " + payload.contract.strike + " " + quoteSide + ".", steps };

	  // dimensionar cantidad por presupuesto: qty = floor(maxInversión / (Ask*100))
	  // La etiqueta del contrato empieza por el precio: "1.63 Strike 360, Call Ask ...".
	  let quotePrice = null;
	  const priceMatch = labelOf(quoteButton).match(/([0-9]+\.[0-9]{2})/);
	  if (priceMatch) quotePrice = Number(priceMatch[1]);
	  let qty = Number(payload.orderPlan.quantity) || 1;
	  if (payload.action === "BUY" && payload.orderPlan.autoSize) {
	    if (quotePrice && quotePrice >= 0.05) {
	      if ((quotePrice * 100) > payload.orderPlan.maxInvestment) {
	        return { ok: false, state: "BUDGET_EXCEEDED", detail: "Ask $" + quotePrice + " supera el presupuesto de $" + payload.orderPlan.maxInvestment + " por operación.", steps };
	      }
	      qty = Math.floor(payload.orderPlan.maxInvestment / (quotePrice * 100));
	      const maxContracts = Number(payload.orderPlan.maxContracts) || 1;
	      qty = Math.min(Math.max(1, maxContracts), Math.max(1, qty));
	      steps.push("Ask " + quotePrice + " → " + qty + " contrato(s) (≤$" + payload.orderPlan.maxInvestment + ")");
	    } else {
	      return { ok: false, state: "ASK_NOT_RELIABLE", detail: "Ask no fiable (" + (quotePrice == null ? "no leído" : "$" + quotePrice + ", mercado cerrado/expirado") + ").", steps };
	    }
	  } else if (payload.action === "SELL") {
	    if (!quotePrice || quotePrice < 0.01) {
	      return { ok: false, state: "BID_NOT_RELIABLE", detail: "Bid no fiable (" + (quotePrice == null ? "no leído" : "$" + quotePrice) + ").", steps };
	    }
	    steps.push("Bid " + quotePrice + " → vender " + qty + " contrato(s)");
	  }

	  click(quoteButton, "Seleccionar " + quoteSide + " " + payload.contract.type + " " + payload.contract.strike);
  await sleep(1400);

  // 6) cantidad + MARKET
  const ticketRoots = () => [document.querySelector("[data-testid='trade-ticket-wrapper']"), document.querySelector(".confirmation-tray-portal-root"), document.querySelector("[data-testid='trade-table']"), document.body].filter(Boolean);
  const rootInputs = () => ticketRoots().flatMap((r) => deepQueryAll("input", r)).filter(visible);
  const rootButtons = () => ticketRoots().flatMap((r) => deepQueryAll("button, [role='button']", r)).filter(visible);
  const rootSelects = () => ticketRoots().flatMap((r) => deepQueryAll("select", r)).filter(visible);
  const optionLabel = (o) => String(o.textContent || o.label || o.value || "").replace(/\s+/g, " ").trim();
  const qtyInput = rootInputs().find((i) => /qty|quantity|cantidad/i.test(labelOf(i))) || deepQueryAll("input[data-testid='trade-quantity-input'], input[aria-label='Quantity Input']").filter(visible)[0];
  let quantityOk = false;
  if (qtyInput) { setValue(qtyInput, String(qty)); pressEnter(qtyInput); steps.push("Cantidad " + qty); await sleep(500); const typed = Number(String(qtyInput.value||"").replace(/,/g,"")); quantityOk = Number.isFinite(typed) ? typed === Number(qty) : true; }
  const setSelectByText = (sel, t) => { const o = [...sel.options].find((x) => new RegExp("\\b"+t+"\\b","i").test(optionLabel(x))); if (!o) return false; sel.value = o.value; sel.dispatchEvent(new Event("input",{bubbles:true})); sel.dispatchEvent(new Event("change",{bubbles:true})); return true; };
  const orderTypeTexts = () => [...rootSelects().map((s) => optionLabel(s.selectedOptions?.[0]||{})), ...rootButtons().filter((b) => /order-type-dropdown-value|Order Type|MARKET|MKT|LIMIT|LMT|type/i.test(labelOf(b))).map(labelOf)].map((t) => String(t).replace(/\s+/g," ").trim()).filter(Boolean);
  const marketIs = () => orderTypeTexts().some((t) => /\b(MARKET|MKT)\b/i.test(t));
  let marketOk = false;
  for (const sel of rootSelects()) { const labels = [...sel.options].map(optionLabel).join(" "); if (/\b(Market|Limit|MKT|LMT)\b/i.test(labels) && setSelectByText(sel, "Market")) { steps.push("Tipo MARKET"); await sleep(350); marketOk = await waitUntil(() => marketIs()?true:null, 4000, 250) ? true : false; break; } }
  if (!marketOk) {
    const btn = rootButtons().find((b) => /order-type-dropdown-value|Order Type/i.test(labelOf(b)) || /\b(MARKET|MKT|LIMIT|LMT)\b/i.test(labelOf(b)));
    if (btn && /\b(MARKET|MKT)\b/i.test(labelOf(btn))) marketOk = true;
    else if (btn) { click(btn, "Abrir tipo de orden"); await sleep(500); const choice = deepQueryAll("[role='option'], [role='menuitem'], li, button").filter(visible).find((i) => /\bMarket\b|\bMKT\b/i.test(labelOf(i))); if (choice) { click(choice, "Seleccionar MARKET"); await sleep(500); marketOk = await waitUntil(() => marketIs()?true:null, 4000, 250) ? true : false; } }
  }

  // 7) candado de seguridad + envío
  if (!payload.submitOrder) return { ok: true, state: "ORDER_PREPARED_NOT_SENT", detail: "Ticket preparado (envío desactivado).", steps, quantityOk, marketOk };
  // Candado de seguridad: bloquear si hay señal Live explícita; permitir si la
  // automatización ya confirmó Paper (login) o si el texto lo confirma aquí.
  if (hasLiveRisk()) return { ok: false, state: "LIVE_RISK_BLOCKED", detail: "BLOQUEADO: la cuenta visible parece Live.", steps };
  if (!payload.paperConfirmed && !isPaperMoney()) return { ok: false, state: "PAPER_NOT_CONFIRMED", detail: "BLOQUEADO: no confirmé Paper Money antes de enviar.", steps };
  if (!marketOk) return { ok: false, state: "MARKET_NOT_CONFIRMED", detail: "No pude confirmar MARKET en el ticket.", steps, quantityOk };

  const actionRoots = () => [...deepQueryAll("[data-testid='trade-ticket-wrapper'], [data-testid*='ticket'], [data-testid*='order-preview'], [data-testid*='review'], [data-testid*='confirmation'], .confirmation-tray-portal-root, [class*='trade-ticket'], [class*='confirmation']")].filter(visible);
  const actionButtons = () => { const roots = actionRoots(); const src = roots.length ? roots : ticketRoots().filter((r) => r !== document.body); return src.flatMap((r) => deepQueryAll("button, [role='button']", r)).filter(visible); };
  const findAction = (pattern) => actionButtons().find((b) => { const l = labelOf(b); return pattern.test(l) && !/cancel|delete|remove|clear|edit|back|modify|show|hide|expand|collapse|option\s*chain|expiration|strike|column|settings|filter/i.test(l) && !b.disabled && b.getAttribute("aria-disabled") !== "true"; });
	  const review = await waitUntil(() => findAction(/\breview\b|review order|\bpreview\b|preview order/i), 9000, 350);
	  if (!review) return { ok: false, state: "REVIEW_BUTTON_NOT_FOUND", detail: "No encontré el botón Review.", steps };
	  click(review, "Review"); await sleep(1600);
	  const send = findAction(/send|submit|place|confirm/i);
	  if (!send) return { ok: false, state: "SEND_BUTTON_NOT_FOUND", detail: "No encontré el botón final para enviar la orden.", steps };
	  click(send, "Confirmar envío paper"); await sleep(1600);
	  const done = findAction(/\bdone\b|\bok\b|\bclose\b/i);
	  if (done) { click(done, "Cerrar confirmación"); await sleep(600); }
	  return { ok: true, state: payload.action === "SELL" ? "PAPER_EXIT_SUBMITTED" : "PAPER_ORDER_SUBMITTED", detail: (payload.action === "SELL" ? "Venta MARKET enviada en Paper Money." : "Compra MARKET enviada en Paper Money."), steps, quantityOk, marketOk, action: payload.action, price: quotePrice, quantity: qty, symbol: payload.symbol, side: payload.contract.type === "put" ? "PUT" : "CALL", strike: payload.contract.strike, expiration: payload.contract.expiration };
	})();
	"""
