"""
Captura de tráfico WebSocket del webview de TOS (paso 1 del pivot a WS).

Inyecta un hook en `window.WebSocket` (en document-creation, también en
subframes) que registra URL + frames enviados/recibidos en `window.__WS_LOG`,
y hace console.log de los frames ENVIADOS (que son pocos e incluyen las órdenes
y suscripciones). Es read-only: no altera el comportamiento de la página.

Activación: TOS_WS_CAPTURE=1 al lanzar el executor. Vuelca a tos/ws_capture.json
periódicamente para analizar el protocolo (endpoint, auth, schema de orden).
"""
from __future__ import annotations

# Hook JS. Se inyecta en cada documento ANTES de que la página cree su WebSocket.
HOOK_JS = r"""
(function () {
  if (window.__WS_HOOKED) return;
  window.__WS_HOOKED = true;
  window.__WS_LOG = [];
  var MAX = 3000;
  function enc(data) {
    try {
      if (typeof data === 'string') return { kind: 'text', data: data.slice(0, 12000), len: data.length };
      if (data instanceof ArrayBuffer) {
        var u = new Uint8Array(data);
        var b = '';
        for (var i = 0; i < Math.min(u.length, 6000); i++) b += String.fromCharCode(u[i]);
        return { kind: 'bin', data: btoa(b), len: data.byteLength };
      }
      if (data && data.byteLength !== undefined) return { kind: 'binview', len: data.byteLength };
      if (data && data.size !== undefined) return { kind: 'blob', len: data.size };
      return { kind: 'other', data: String(data).slice(0, 2000) };
    } catch (e) { return { kind: 'err', data: String(e) }; }
  }
  function rec(dir, url, data) {
    var e = enc(data);
    e.t = Date.now(); e.dir = dir; e.url = url;
    window.__WS_LOG.push(e);
    if (window.__WS_LOG.length > MAX) window.__WS_LOG.shift();
    if (dir === 'send') {
      try { console.log('WSCAP-SEND ' + url + ' :: ' + (e.kind === 'text' ? e.data : '[' + e.kind + ' ' + e.len + 'B] ' + (e.data || '').slice(0, 400))); } catch (x) {}
    }
  }
  var Native = window.WebSocket;
  function Hooked(url, protocols) {
    var ws = protocols !== undefined ? new Native(url, protocols) : new Native(url);
    rec('open', url, '');
    try {
      var send = ws.send.bind(ws);
      ws.send = function (d) { rec('send', url, d); return send(d); };
      ws.addEventListener('message', function (ev) { rec('recv', url, ev.data); });
      ws.addEventListener('close', function () { rec('close', url, ''); });
    } catch (e) {}
    return ws;
  }
  Hooked.prototype = Native.prototype;
  ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED'].forEach(function (k) { Hooked[k] = Native[k]; });
  window.WebSocket = Hooked;
  console.log('WSCAP: hook de WebSocket instalado');
})();
"""

# JS que devuelve el log capturado como string JSON (para volcarlo a disco).
DUMP_JS = "JSON.stringify(window.__WS_LOG || [])"


def build_script():
    """Crea el QWebEngineScript a inyectar en el perfil (document-creation, subframes)."""
    from PySide6.QtWebEngineCore import QWebEngineScript
    sc = QWebEngineScript()
    sc.setName("ws_capture")
    sc.setSourceCode(HOOK_JS)
    sc.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
    sc.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
    sc.setRunsOnSubFrames(True)
    return sc
