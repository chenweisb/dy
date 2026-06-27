/**
 * 抖音 SSL bypass — 纯 native（抖音屏蔽 Frida Java 桥）
 * 推荐: python run_spy.py  （spawn 冷启动）
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

  function findExport(lib, name) {
    try {
      if (!lib) return Module.findGlobalExportByName(name);
      var mod = Process.findModuleByName(lib);
      return mod ? mod.findExportByName(name) : null;
    } catch (e) {
      return null;
    }
  }

  // 只 hook 网络相关 so，不扫描整个 APK（避免刷屏和误 hook）
  function isNetworkModule(mod) {
    if (!mod || !mod.name) return false;
    return /ttnet|sscronet|ttboringssl|cronet|vcn|boringssl/i.test(mod.name);
  }

  var nativeHooked = {};
  var javaDone = false;
  var readyLogged = false;
  var networkLibsLogged = false;

  var trustVerifyCb = new NativeCallback(
    function (ssl, out_alert) {
      return 0;
    },
    "int",
    ["pointer", "pointer"]
  );

  var SSL_FUNCS = [
    "SSL_CTX_set_custom_verify",
    "SSL_set_custom_verify",
    "SSL_get_verify_result",
    "SSL_CTX_set_verify",
    "SSL_set_verify",
    "SSL_CTX_set_cert_verify_callback",
  ];

  function markNativeReady(extra) {
    if (readyLogged) return;
    readyLogged = true;
    log("native 就绪 — " + (extra || "请进商城点商品"));
  }

  function hookNativeSsl() {
    var hooked = 0;
    var netLibs = [];

    function attachAt(name, modName, addr) {
      var key = name + "@" + modName;
      if (nativeHooked[key] || !addr) return false;
      try {
        if (name.indexOf("set_custom_verify") !== -1 || name.indexOf("cert_verify_callback") !== -1) {
          Interceptor.attach(addr, {
            onEnter: function (args) {
              if (args[2]) args[2] = trustVerifyCb;
            },
          });
        } else if (name.indexOf("set_verify") !== -1) {
          Interceptor.attach(addr, {
            onEnter: function (args) {
              if (args[1]) args[1] = trustVerifyCb;
            },
          });
        } else if (name === "SSL_get_verify_result") {
          Interceptor.attach(addr, {
            onLeave: function (retval) {
              retval.replace(0);
            },
          });
        }
        nativeHooked[key] = true;
        hooked++;
        log("native " + name + " @" + modName);
        if (/ttnet/i.test(modName)) markNativeReady("libttnet 已 hook");
        return true;
      } catch (e) {
        return false;
      }
    }

    function attachX509(mod) {
      var key = "X509_verify_cert@" + mod.name;
      if (nativeHooked[key]) return;
      var addr = mod.findExportByName("X509_verify_cert");
      if (!addr) return;
      try {
        Interceptor.attach(addr, {
          onLeave: function (retval) {
            if (retval.toInt32() <= 0) retval.replace(1);
          },
        });
        nativeHooked[key] = true;
        hooked++;
        log("native X509_verify_cert @" + mod.name);
        if (/ttnet/i.test(mod.name)) markNativeReady("libttnet 已 hook");
      } catch (e) {}
    }

    Process.enumerateModules().forEach(function (mod) {
      if (!isNetworkModule(mod)) return;
      netLibs.push(mod.name);
      SSL_FUNCS.forEach(function (name) {
        attachAt(name, mod.name, mod.findExportByName(name));
      });
      attachX509(mod);
    });

    if (!networkLibsLogged) {
      networkLibsLogged = true;
      log("网络 so: " + (netLibs.length ? netLibs.join(", ") : "暂无"));
      if (netLibs.join(",").indexOf("ttnet") === -1) {
        log("⚠ 尚无 libttnet.so，进商城后若出现「so 加载: libttnet.so」即正常");
      }
    }

    if (hooked > 0 && !readyLogged) markNativeReady("请进商城点商品");
    return hooked;
  }

  function scheduleNativeRetries() {
    var round = 0;
    var idle = 0;
    var timer = setInterval(function () {
      round++;
      var n = hookNativeSsl();
      if (n > 0) {
        idle = 0;
        log("native 补 hook +" + n);
      } else {
        idle++;
      }
      if (idle >= 5 || round >= 30) clearInterval(timer);
    }, 2000);
  }

  function hookNativeOnModuleLoad() {
    var dlopen = findExport(null, "android_dlopen_ext") || findExport(null, "dlopen");
    if (!dlopen) return;
    Interceptor.attach(dlopen, {
      onEnter: function (args) {
        try {
          this.path = args[0].readUtf8String() || "";
        } catch (e) {
          this.path = "";
        }
      },
      onLeave: function () {
        if (!this.path) return;
        var base = this.path.split("/").pop();
        if (isNetworkModule({ name: base, path: this.path })) {
          log("so 加载: " + base);
          hookNativeSsl();
        }
      },
    });
    log("dlopen watcher OK");
  }

  function runJavaHooks() {
    if (javaDone) return;
    javaDone = true;
    try {
      Java.use("com.ttnet.org.chromium.net.impl.CronetEngineBuilderImpl")
        .enablePublicKeyPinningBypassForLocalTrustAnchors.implementation = function () {
          return this.enablePublicKeyPinningBypassForLocalTrustAnchors(true);
        };
    } catch (e) {}
    log("Java 层就绪");
  }

  function tryJavaBackground() {
    var n = 0;
    var timer = setInterval(function () {
      n++;
      try {
        if (typeof Java !== "undefined") {
          Java.perform(runJavaHooks);
          clearInterval(timer);
          return;
        }
      } catch (e) {}
      if (n >= 3) clearInterval(timer);
    }, 1000);
  }

  tryHook("NativeSSL", hookNativeSsl);
  tryHook("DlopenWatcher", hookNativeOnModuleLoad);
  scheduleNativeRetries();
  tryJavaBackground();
})();
