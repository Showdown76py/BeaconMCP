// Minimal WebAuthn plumbing shared by the dashboard auth pages.
//
// The server speaks base64url (that's what py_webauthn's options_to_json
// emits and what its verifiers parse); the browser API speaks ArrayBuffer.
// Everything here is that translation, plus the two ceremony wrappers.
window.BeaconPasskeys = (function() {
  "use strict";

  function supported() {
    return !!(window.PublicKeyCredential && navigator.credentials &&
              navigator.credentials.create && navigator.credentials.get);
  }

  function b64urlToBuf(value) {
    var s = String(value).replace(/-/g, "+").replace(/_/g, "/");
    while (s.length % 4) s += "=";
    var bin = window.atob(s);
    var bytes = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes.buffer;
  }

  function bufToB64url(buf) {
    var bytes = new Uint8Array(buf);
    var bin = "";
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return window.btoa(bin)
      .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function decodeDescriptors(list) {
    return (list || []).map(function(d) {
      var out = { type: d.type || "public-key", id: b64urlToBuf(d.id) };
      if (d.transports && d.transports.length) out.transports = d.transports;
      return out;
    });
  }

  // navigator.credentials.create() — returns a JSON-safe attestation.
  function register(options) {
    var publicKey = Object.assign({}, options);
    publicKey.challenge = b64urlToBuf(options.challenge);
    publicKey.user = Object.assign({}, options.user, {
      id: b64urlToBuf(options.user.id),
    });
    publicKey.excludeCredentials = decodeDescriptors(options.excludeCredentials);
    return navigator.credentials.create({ publicKey: publicKey }).then(function(cred) {
      if (!cred) throw new Error("No credential was created.");
      var response = cred.response;
      var out = {
        id: cred.id,
        rawId: bufToB64url(cred.rawId),
        type: cred.type,
        clientExtensionResults: cred.getClientExtensionResults
          ? cred.getClientExtensionResults() : {},
        response: {
          clientDataJSON: bufToB64url(response.clientDataJSON),
          attestationObject: bufToB64url(response.attestationObject),
        },
      };
      if (cred.authenticatorAttachment) {
        out.authenticatorAttachment = cred.authenticatorAttachment;
      }
      if (response.getTransports) {
        try { out.response.transports = response.getTransports(); } catch (e) {}
      }
      return out;
    });
  }

  // navigator.credentials.get() — returns a JSON-safe assertion.
  function authenticate(options) {
    var publicKey = Object.assign({}, options);
    publicKey.challenge = b64urlToBuf(options.challenge);
    publicKey.allowCredentials = decodeDescriptors(options.allowCredentials);
    return navigator.credentials.get({ publicKey: publicKey }).then(function(cred) {
      if (!cred) throw new Error("No passkey was selected.");
      var response = cred.response;
      return {
        id: cred.id,
        rawId: bufToB64url(cred.rawId),
        type: cred.type,
        clientExtensionResults: cred.getClientExtensionResults
          ? cred.getClientExtensionResults() : {},
        response: {
          clientDataJSON: bufToB64url(response.clientDataJSON),
          authenticatorData: bufToB64url(response.authenticatorData),
          signature: bufToB64url(response.signature),
          userHandle: response.userHandle ? bufToB64url(response.userHandle) : null,
        },
      };
    });
  }

  // The browser throws opaque DOMExceptions; turn the ones users actually
  // hit into something actionable and let the rest through as-is.
  function describeError(err) {
    if (!err) return "Passkey request failed.";
    if (err.name === "NotAllowedError") {
      return "Passkey prompt cancelled or timed out.";
    }
    if (err.name === "InvalidStateError") {
      return "This device already has a passkey registered for this client.";
    }
    if (err.name === "SecurityError") {
      return "Passkeys need a secure origin (HTTPS or localhost).";
    }
    return err.message || String(err);
  }

  return {
    supported: supported,
    register: register,
    authenticate: authenticate,
    describeError: describeError,
    b64urlToBuf: b64urlToBuf,
    bufToB64url: bufToB64url,
  };
})();
