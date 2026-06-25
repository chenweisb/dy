/**
 * 抖音 Android SSL Pinning bypass（Java + Native，配合 mitmproxy）
 * 用法: python run_spy.py  （冷启动 spawn，勿用 --attach）
 */
(function () {
  "use strict";

  function log(msg) {
    send("[ssl_bypass] " + msg);
  }

  function tryHook(label, fn) {
    try {
      fn();
      log(label + " OK");
      return true;
    } catch (e) {
      log(label + " skip: " + e);
      return false;
    }
  }

  // ---------- Native (Cronet/BoringSSL，商城走这层) ----------
  function hookNativeSsl() {
    var hooked = 0;

    function attachVerify(name, lib) {
      var addr = Module.findExportByName(lib, name);
      if (!addr) return;
      try {
        Interceptor.attach(addr, {
          onEnter: function (args) {
            if (name.indexOf("set_custom_verify") !== -1 && args[2]) {
              var cb = new NativeCallback(
                function (ssl, out_alert) {
                  return 0;
                },
                "int",
                ["pointer", "pointer"]
              );
              args[2] = cb;
            }
          },
        });
        hooked++;
        log("native " + name + " @" + (lib || "null"));
      } catch (e) {
        log("native " + name + " fail: " + e);
      }
    }

    ["SSL_CTX_set_custom_verify", "SSL_set_custom_verify"].forEach(function (name) {
      attachVerify(name, null);
      attachVerify(name, "libttnet.so");
      attachVerify(name, "libssl.so");
      attachVerify(name, "libboringssl.so");
    });

    var getVerify = Module.findExportByName(null, "SSL_get_verify_result");
    if (getVerify) {
      Interceptor.replace(
        getVerify,
        new NativeCallback(
          function () {
            return 0;
          },
          "long",
          ["pointer"]
        )
      );
      hooked++;
      log("native SSL_get_verify_result -> 0");
    }

    if (hooked === 0) {
      log("native: 未找到 SSL 符号，依赖 Java 层 hook");
    }
  }

  // ---------- Java ----------
  function hookTrustManagers() {
    var TrustManagerImpl = Java.use("com.android.org.conscrypt.TrustManagerImpl");

    TrustManagerImpl.verifyChain.implementation = function (
      untrustedChain,
      trustAnchorChain,
      host,
      clientAuth,
      ocspData,
      tlsSctData
    ) {
      return untrustedChain;
    };

    if (TrustManagerImpl.checkTrustedRecursive) {
      TrustManagerImpl.checkTrustedRecursive.overloads.forEach(function (overload) {
        overload.implementation = function () {
          return arguments[0];
        };
      });
    }

    TrustManagerImpl.checkServerTrusted.overloads.forEach(function (overload) {
      overload.implementation = function () {
        var ret = overload.returnType.className;
        if (ret.indexOf("[") === 0) {
          return arguments[0];
        }
      };
    });
  }

  function hookSslContext() {
    var X509TrustManager = Java.use("javax.net.ssl.X509TrustManager");
    var SSLContext = Java.use("javax.net.ssl.SSLContext");

    var TrustAll = Java.registerClass({
      name: "com.dyspy.TrustAllManager2",
      implements: [X509TrustManager],
      methods: {
        checkClientTrusted: function () {},
        checkServerTrusted: function () {},
        getAcceptedIssuers: function () {
          return [];
        },
      },
    });

    SSLContext.init.overload(
      "[Ljavax.net.ssl.KeyManager;",
      "[Ljavax.net.ssl.TrustManager;",
      "java.security.SecureRandom"
    ).implementation = function (km, tm, sr) {
      this.init(km, [TrustAll.$new()], sr);
    };
  }

  function hookOkHttp() {
    ["okhttp3.CertificatePinner", "com.squareup.okhttp.CertificatePinner"].forEach(function (cls) {
      try {
        var Pinner = Java.use(cls);
        Pinner.check.overloads.forEach(function (ol) {
          ol.implementation = function () {};
        });
        log(cls + " OK");
      } catch (e) {
        log(cls + " skip");
      }
    });
  }

  function hookHostnameVerifier() {
    var HV = Java.registerClass({
      name: "com.dyspy.TrustAllHostnameVerifier2",
      implements: [Java.use("javax.net.ssl.HostnameVerifier")],
      methods: {
        verify: function () {
          return true;
        },
      },
    });
    var HUC = Java.use("javax.net.ssl.HttpsURLConnection");
    HUC.setDefaultHostnameVerifier.implementation = function () {
      this.setDefaultHostnameVerifier(HV.$new());
    };
    HUC.setHostnameVerifier.implementation = function () {
      this.setHostnameVerifier(HV.$new());
    };
  }

  function hookCronetJava() {
    try {
      var Builder = Java.use("com.ttnet.org.chromium.net.impl.CronetEngineBuilderImpl");
      Builder.enablePublicKeyPinningBypassForLocalTrustAnchors.implementation = function () {
        return this.enablePublicKeyPinningBypassForLocalTrustAnchors(true);
      };
    } catch (e) {
      log("CronetEngineBuilder skip");
    }

    ["com.ttnet.org.chromium.net.AndroidNetworkLibrary", "com.ttnet.org.chromium.net.X509Util"].forEach(
      function (cls) {
        try {
          var Lib = Java.use(cls);
          Lib.verifyServerCertificates.overloads.forEach(function (ol) {
            ol.implementation = function () {
              return arguments[0];
            };
          });
          log(cls + " OK");
        } catch (e) {
          log(cls + " skip");
        }
      }
    );
  }

  function hookOk3TlsCallback() {
    try {
      var CB = Java.use("com.bytedance.frameworks.baselib.network.http.ok3.impl.Ok3TlsProcessCallback");
      CB.verify.overloads.forEach(function (ol) {
        ol.implementation = function () {
          return true;
        };
      });
      log("Ok3TlsProcessCallback OK");
    } catch (e) {
      log("Ok3TlsProcessCallback skip");
    }
  }

  function hookNetworkSecurityPolicy() {
    var NSP = Java.use("android.security.NetworkSecurityPolicy");
    NSP.isCleartextTrafficPermitted.overloads.forEach(function (ol) {
      ol.implementation = function () {
        return true;
      };
    });
  }

  // Native 尽早装，不等 Java.perform
  tryHook("NativeSSL", hookNativeSsl);

  Java.perform(function () {
    log("Java.perform start");
    tryHook("TrustManagers", hookTrustManagers);
    tryHook("SslContext", hookSslContext);
    hookOkHttp();
    tryHook("HostnameVerifier", hookHostnameVerifier);
    hookCronetJava();
    hookOk3TlsCallback();
    tryHook("NetworkSecurityPolicy", hookNetworkSecurityPolicy);
    log("就绪 — 现在可以进商城；mitmproxy 不应再出现 certificate unknown");
  });
})();
