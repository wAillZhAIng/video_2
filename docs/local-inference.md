# 本地 MiniMax H3 Ref2VA 推理节点

应用容器负责网页、API、任务、SQLite、FFmpeg 和结果保存。ComfyUI、MiniMax H3 Ref2VA 节点、模型权重、CUDA 与 GPU 驱动由用户在自己的推理主机维护。

本地视频路径是直连原生 H3：已冻结的镜头脚本、图片参考与可选参考视频帧直接进入已验证的 ComfyUI 工作流。此部署工具只管理本地 ComfyUI 进程；它不会启动中间代理、独立语言模型或额外的脚本解释服务。

## 前提条件

- NVIDIA GPU、驱动和与所选 MiniMax H3 节点兼容的 ComfyUI Python 环境；Windows NVIDIA 为当前本地基线。Docker 应用能跨平台运行，不代表 macOS 或 Linux 本机 GPU 已完成同等出片验收。
- 已从各自官方来源安装 ComfyUI、MiniMax H3 Ref2VA 节点、H3 模型/编码器/VAE；Fast 还需要与当前工作流匹配的 SMD 4-step LoRA。
- 可调用的 `ffmpeg` 和 `ffprobe`，以及足够的磁盘空间给模型、输入、输出和缓存。
- 独立的可写运行目录。工具不会下载、移动或删除模型权重。

请先在当前 ComfyUI 安装中确认节点接口。应用会在真实派发前读取 `/object_info`，核对 `MiniMaxH3ReferenceToVideo`、图片输入、`LoadVideo`、`GetVideoComponents` 与可增长的参考视频插槽。只有节点 schema 和工作流匹配时，才会上传和排队任务。

## 建立受控运行目录

以下示例不修改系统 Python、PATH 或已有 ComfyUI 配置。路径可替换为自己的位置：

```powershell
& C:\AI\envs\comfy\Scripts\python.exe -B .\scripts\local_inference\videofactory_native.py init `
  --comfy-root C:\AI\ComfyUI `
  --runtime-root C:\AI\VideoFactoryRuntime `
  --comfy-python C:\AI\envs\comfy\Scripts\python.exe `
  --model-root C:\AI\Models `
  --comfy-origin http://127.0.0.1:8188
```

该命令创建 `C:\AI\VideoFactoryRuntime\native-runtime.json`、独立的输入/输出/日志目录和 ComfyUI 用户目录。它不覆盖已有模型或工作流。若 FFmpeg 不在 `PATH`，增加 `--ffmpeg-path` 和 `--ffprobe-path` 指向实际可执行文件。

## 检查并启动 ComfyUI

```powershell
& C:\AI\envs\comfy\Scripts\python.exe -B .\scripts\local_inference\videofactory_native.py check `
  --config C:\AI\VideoFactoryRuntime\native-runtime.json

& C:\AI\envs\comfy\Scripts\python.exe -B .\scripts\local_inference\videofactory_native.py start-comfy `
  --config C:\AI\VideoFactoryRuntime\native-runtime.json
```

`check` 验证 ComfyUI 主程序、Python、运行目录和 FFmpeg/FFprobe；`start-comfy` 只在该 loopback ComfyUI 未运行时启动一个进程，并把日志写到 `RuntimeRoot\logs`。它不会中断已有 ComfyUI 任务或接管其他进程。

检查通过不等于模型已经出片。节点和模型兼容性由应用在每次提交前针对真实 `/object_info` 和工作流核对；仍需完成一次短镜头的真实生成、媒体解码和人工画面审核。

## 在网页连接 ComfyUI

在 **系统设置 → 添加服务 → 视频生成 → 本地 / 私有 ComfyUI** 中选择 **MiniMax H3 原生 Ref2VA：Fast 4 / 标准 20 / 高质量 30**，填写应用可访问的 ComfyUI origin，然后勾选“确认这是我管理的本地模型服务”。

- Docker Desktop 的默认本地服务档案使用 `http://host.docker.internal:8189`。该端口必须实际转发到你管理的 ComfyUI；容器内的 `127.0.0.1` 指向容器自身。
- 直接在 GPU 主机运行应用时，使用实际的 ComfyUI 监听 origin，例如上例的 `http://127.0.0.1:8188`。
- 跨机器连接时，只使用自己的受控私网地址、认证/防火墙规则和最小必要访问范围。不要把 ComfyUI 暴露到公共互联网。

地址变更后需要重新确认。连接测试通过只证明地址、节点和模型接口符合适配器要求，不代替视频生成。

## 原生参考媒体输入

项目级参考视频可被镜头覆盖。图片和视频的身份/用途彼此独立：

- 脚本决定目标内容；产品或人物图片决定视觉身份。
- 视频可选择动作、运镜、画面风格或综合参考；默认只提供视觉参考，不复制原音轨。
- 参考视频会先用 FFprobe 验证格式、编码、时长、帧率、解码性与 SHA256，再保留原文件并创建 24 FPS 的模型输入副本。
- 原生 H3 输入限制由已安装节点和显存共同决定；当前界面会显示并验证最多 9 张图片、最多 3 段视频、单段 2–15 秒、视频总时长不超过 15 秒及混合参考总数不超过 12 的规则。

真实工作流在 `workflows/h3_ref2va_native_fast4_v2.json`、`workflows/h3_ref2va_native_quality20_v2.json` 和 `workflows/h3_ref2va_native_quality30_v2.json`。参考视频只有在实际 `LoadVideo`/`GetVideoComponents` 输出连接到 H3 的 `ref_video_N` 插槽时才会被接受；应用不会用一张截图或提示词中的文件路径伪装视频条件输入。

## 首次验收和故障定位

1. 在网页保存服务，运行连接测试并确认实际节点诊断。
2. 导入一个短剧本；分别尝试纯脚本、脚本加图片、脚本加视频，以及图片加视频。
3. 对每次实际任务检查冻结快照、ComfyUI prompt ID、输出 MP4、时长/帧率/画幅/解码和超分结果。
4. 在成片区域播放和下载最终 MP4，再人工核对商品外观、动作、运镜、声音和闪烁。

当节点、模型或显存检查失败时，保留 ComfyUI 日志和应用给出的脱敏错误；不要用重新提交、修改数据库、删除历史或暴露服务端口来规避问题。升级节点或工作流前等待队列空闲，备份运行目录和 Docker 数据卷，更新后重新检查并重新做小样验收。
