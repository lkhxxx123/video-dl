# 发布说明（exe 绿色便携包）

## 构建（开发机执行一次）

```cmd
packaging\build_exe.bat
```

产物 `dist\视频下载器\`。**把整个文件夹压缩为 zip** 即可分发。

### 构建内容

| 组件 | 来源 | 大小 |
|---|---|---|
| 视频下载器.exe + _internal/ | PyInstaller（含 Python 运行时、playwright driver） | ~80MB |
| runtime/browsers/ | 构建机 `playwright install chromium` 的产物拷入 | ~400MB |
| runtime/tools/ | ffmpeg.exe + ffprobe.exe（静态版） | ~200MB |

zip 压缩后约 400-500MB，解压后 600-800MB。

## 用户使用

1. 解压 zip 到任意目录（**路径建议不含特殊字符**）
2. 双击 `视频下载器.exe`（会同时弹出控制台窗口——扫码/滑块提示在那里，别关）
3. 首次使用：
   - 点顶栏「AI 配置」填入 API Key（或手动在 exe 旁放 `key.txt`）
   - 点「扫码登录」完成抖音登录（登录态存 exe 旁 `.browser-profile\`）
4. 所有数据（下载/登录态/配置）都在 exe 旁边的文件夹里——**拷走文件夹=搬走全部数据**

## 已知限制

- **SmartScreen 蓝色警告**：未签名 exe 首次运行必现，点「更多信息 → 仍要运行」；
  彻底解决需购买代码签名证书（年费数千元）
- **杀软误报**：PyInstaller 打包产物偶发被误报，加白名单即可
- **散片模式独立登录态**：首次跑散片任务时浏览器会要求再扫一次码
  （独立 profile，登录一次管 1-2 周）
- ffmpeg 缺失时水印判定不可用——UI 的 `doctor` 自检会明确指出

## 故障排查

| 现象 | 处理 |
|---|---|
| 双击后控制台一闪而过 | 在 cmd 里进文件夹执行 `视频下载器.exe` 看报错 |
| 「Chromium 未下载」 | runtime\browsers 目录是否完整（重新解压 zip） |
| 水印判定全部报错 | runtime\tools 缺 ffmpeg/ffprobe，按提示补放 |
| 任务一直「排队中」 | 同平台已有任务在跑（跑完自动接力），属正常调度 |
