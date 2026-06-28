# 抖音商城抓包工具

## 方式一：EXE 版（推荐给别人，无需 Python）

在本机（需 Python）打包一次：

```powershell
powershell -ExecutionPolicy Bypass -File build_exe.ps1
```

生成 `release\dy_bao_exe.zip`，解压后 **双击 `启动.exe`** 即可。  
内含 mitmproxy + frida，**不用安装 Python**。

## 方式二：脚本版（一键启动.bat）

解压 `dy_bao_toolkit.zip`，双击 **`一键启动.bat`** / **`START.bat`**。  
首次自动下载便携 Python（需联网）。

## 输出

- `baojs/{商品ID}.json`
- `baotxt/{商品ID}.txt`（王者上货格式）

## 手机端（必做）

Root + frida-server + WiFi 代理 `:8080` + mitm 证书（小米需 Magisk 移证书）

详细见 `QUICKSTART.txt`

