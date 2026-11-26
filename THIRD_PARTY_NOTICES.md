# 第三方声明

应用自身的许可由仓库根目录 `LICENSE` 确定。它不替代依赖、容器基础镜像、模型权重、外部服务及用户素材各自的许可。本项目不分发模型权重、用户业务数据或参考素材。

## Comfy-Org 工作流模板

以下文件包含 Comfy-Org 官方模板或由其节点图修改的接口图：

- `workflows/h3_r2v_official_ui.json`
- `workflows/h3_r2v_character_v1.json`
- `workflows/arcreel_minimax_h3_r2v.endpoint.json`（保留已有兼容文件名）

来源：[Comfy-Org/workflow_templates](https://github.com/Comfy-Org/workflow_templates/tree/e7cd011d4ded3411c2f481200544f0be6fdc962e)，固定提交 `e7cd011d4ded3411c2f481200544f0be6fdc962e`。完整 MIT 版权声明见 `licenses/Comfy-Org-workflow-templates-MIT.txt`。接口图调整了输入图像、提示词和保存文件名前缀；其运行节点来自官方模板。另一个独立的 ArcReel 项目及其代码没有打包在本仓库中。

## 原生 MiniMax H3 Ref2VA 工作流

`workflows/h3_ref2va_native_fast4_v2.json`、`workflows/h3_ref2va_native_quality20_v2.json` 和 `workflows/h3_ref2va_native_quality30_v2.json` 是本项目的 API 工作流封装，目标是当前安装的原生 ComfyUI MiniMax H3 Ref2VA 节点。它们不打包第三方节点、模型、权重或用户素材。Fast 保留 SMD 4-step LoRA 配方，两个 Quality 工作流各自固定质量档位；实际节点和模型兼容性必须由用户的 ComfyUI `/object_info` 验证。

MiniMax H3、SMD 适配器和 ComfyUI 节点适用各自的上游许可与使用条款。保留本说明不代表分发模型权重或授予模型使用权。

## Python、JavaScript 和系统组件

锁定依赖以 `backend/requirements-docker.lock`、`frontend/package-lock.json` 为准。它们有各自的 MIT、BSD、Apache、ISC、MPL、PSF 等许可，不能整体视为应用许可证。发行镜像保留 Debian `/usr/share/doc/*/copyright`，Python 包的 `.dist-info` 许可元数据，以及 Nginx 基础镜像提供的声明。前端依赖声明与源码来源另见 `licenses/`。

后端通过独立进程调用 FFmpeg。Debian FFmpeg 包及启用的 x264 组件涉及 GPL；[FFmpeg 许可说明](https://ffmpeg.org/legal.html)是其上游依据。构建步骤收集实际安装的 FFmpeg/x264 对应源代码归档、校验和、包版本及构建参数到 `/usr/share/video-factory/corresponding-source`，构建失败时不会伪称该步骤通过。该目录可用 `docker compose cp backend:/usr/share/video-factory/corresponding-source ./corresponding-source` 导出。其他系统组件仍适用其各自声明及源码获取方式；这份清单不是整套供应链的法律认证。

## 模型与服务

ComfyUI 插件、MiniMax、语音、超分及云端供应商不随通用 CPU 应用镜像安装。请另行检查对应版本的模型许可、硬件条件、部署地域和服务条款。配置一个服务地址不授予其模型或内容使用权。
