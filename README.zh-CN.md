# Ablation

本地 Prompt 调试工作台：导入请求或日志，修改候选，回放对比，用 GitHub 风格 diff 和行内评论检查证据。

CLI 与 GUI 使用同一份实验记录。可选 `/ablation` skill 从对话上下文识别已有请求、日志或实验。工具自带数据转换和 replay，可以直接使用 CLI，也可以由 Agent 配合 skill 操作。

正式发行包通过 PyPI 和 GitHub Releases 分发。安装和更新行为见下文“安装、数据与更新”。

```sh
uv tool install ablation-workbench
ablation demo --open
```

也可下载 Release 中的 wheel，运行 `uv tool install /path/to/ablation_workbench-0.1.0-py3-none-any.whl`；`pipx install .` 同样支持源码安装。示例全部为人工构造，不会请求模型；未运行的输出明确显示为未测试。

## 使用方式

1. 使用 `ablation config profile --help` 配置数据源与推理端点。配置只保存环境变量名，凭证由使用者自行提供；查询日志与执行推理使用各自凭证。
2. 用 `ablation import request.json --format request` 导入原始请求；`--format log` 用于日志，`--format capture` 用于包含 request / response 的 JSON 记录。不同日志结构通过 profile 的 JSON Pointer 映射配置。
3. 用 `ablation gui` 打开工作台。首屏左侧固定为导入的原始数据，右侧展示修改后的实际差异，包括提示词、工具描述与输入消息。其他实验版本和创建候选、场景的入口放在“运行记录”中；主视图只保留“实际改动”和“输出结果”。
4. 在相同场景下回放原版和候选。查看实际请求、响应及断言结果；没有断言的运行显示为未评分。
5. 在变更行旁点 `+` 添加评论，批量提交。可复制到当前对话，也可交给已配置的 Agent。

候选说明统一写两句：先交代为什么改，再说明大概修改位置及影响，附核实过的 GitHub 源码链接；来源或位置没查清时注明待确认。CLI 用 `--rationale`，或用 `--rationale-file rationale.md` 读取多行说明；GUI 在 diff 下显示同一段文字和可点击的源码链接。

候选可用 `ablation change-groups <experiment> --candidate <candidate> --file groups.json` 补充需求分组，不改变请求、回放或评论锚点。分组包含 `id`、`title`、中文 `reason`、`effect`、精确文本锚点 `matches` 和源码链接 `sources`（每项为 `label`、`url`）。锚点将关联的 diff 改动段合为一项需求；冲突或未命中的分组会报错。GUI 默认按需求展示，切换“按原文”仍使用逐行高亮和行内评论。源码链接仅接受 GitHub 文件直达链接。

首屏汇总统计所选版本与场景的全部历史回放，后一次通过不会掩盖之前的失败。响应 diff 展示最近完成的输出，更早的证据可在运行记录中查看。

会话侧栏默认折叠，展开后按标题和完整会话 ID 列出已保存实验，支持搜索和查看回放次数。会话保留全部批次、场景、重复运行、结果及日志链接，首屏优先展示 diff，后面的“运行记录”页按批次展示完整历史。列表按批次紧凑展示回放，点击整行即可打开较窄的右侧全高抽屉，同批的多次回放在抽屉顶部切换，可查看 Messages、断言和原始 JSON；消息时间线包含已保存的输入、工具调用及返回、可点击放大的截图缩略图和本次模型输出，保留外部日志链接用于溯源。消息区域独立滚动并以 90% 比例显示；打开回放时默认定位到轻微高亮的本次输出，也可随时点击定位入口返回。消息视图复用 Bifrost 的 Apache-2.0 组件，固定来源版本、适配说明和许可证随资源打包。界面复用 Voyage 组件，默认使用 GitHub 主题；右上角标准 VoyageToolbar 提供明暗和主题切换，反馈及语言入口按标准未接入状态禁用。样式和工具栏随工具打包，离线可用。运行 `npm run build:ui` 同步固定版本的资源，`npm run check:ui` 检查一致性。

完整配置、命令与示例见 [English README](README.md)。

## 接入数据源

请求文件可以直接导入；HTTP 记录可使用 `request.body` 和 `response.body` 保存 JSON 正文。日志默认读取 `params` 中的请求字段，并用顶层 `model`、`input`、`messages`、`tools` 覆盖同名字段。任意嵌套结构可配置 `--source-request-pointer` 选出完整请求，或通过 `--source-fields-json` 将顶层请求字段映射到原始记录中的 JSON Pointer。`--source-response-pointer` 用于历史响应；没有响应时省略。映射路径不存在会报错。

远程 JSON 数据源需要显式配置 `--source-base-url`，再提供同 origin 的完整 JSON URL，或用 `--source-path '/records/{ref}'` 将记录引用拼接成接口路径。`ablation import record-123 --ref --format log --profile logs` 强制把输入当作引用。来源认证选择 `--token-env`，或 `--source-user-env` / `--source-password-env`；模型推理独立使用 `--base-url` / `--api-key-env`。这些环境变量的值由使用者提供。

网页分享链接不一定是 JSON 接口。可以先用已有工具导出 JSON，再从文件、stdin 或 GUI 上传导入。原始记录与实际回放请求分别保存，转换说明和 diff 展示还原过程。

## 评论与继续运行

询问“为什么这样改”时选择“追问解释”，解释写回原评论，不产生新候选或新运行。明确要求修改时选择“请求修改”，Agent 创建下一版并按要求验证。默认候选保存在实验中；要应用到业务仓库时，由用户明确要求 Agent 操作相应代码。

点击已保存评论的“继续追问”会保留当时的原文和证据，即使后来重新回放也可以继续讨论旧结果。尚未提交的旧响应草稿仍需核对变化后再提交。

自动模式先配置一次：

```sh
ablation agent configure --backend codex --cwd /path/to/project
```

其他 Agent 可以配置读取 stdin 的命令。复制模式不需要 ablation 启动 Agent，手工粘贴后，Agent 通过 `ablation review reply` 把回答写回 GUI。这里没有宣称可以直接唤醒正在使用的桌面聊天。

## 证据与范围

- 原始日志与重建后的请求分别保留，展示重建差异和缺失字段。
- 测试场景标明真实来源或人工构造，写明修改的条件，避免相同 Query 掩盖输入差异。
- Prompt、输入、完整请求、输出、断言分别展示；旧候选保留，便于回看过拟合改动。
- 回放只验证单次模型请求，不执行模型返回的工具调用，也不证明外部操作已完成。
- 运行次数、独立场景数和断言结果分别记录，小样本全通过不等于统计稳定性证明。

## 安装、数据与更新

GUI 随工具打包，不需要安装 Node。`skills/ablation` 可以手工安装到 Agent 的 skill 目录；当前不做 skill 自动安装或版本同步。

工作区默认在 `~/.local/share/ablation`，可用 `--workspace` 指定。实验和评论存储在本地，支持导出导入；导出文件可能包含原始提示词和模型响应，共享前检查内容。

正式发行包通过 PyPI 和 GitHub Releases 分发。GUI 后台定期检查线上版本，检查结果缓存 24 小时；页面只读缓存，查询慢或断网不会阻塞界面，失败时保留上次已知版本。发现新版后通过 SSE 通知页面，显示升级命令和发布说明，不会自动安装。升级并重启 GUI 后，原页面自动重连并提示刷新，避免打断编辑。源码或 editable 安装跳过检查，`ABLATION_UPDATE_CHECK=0` 可关闭检查。首次发布需要配置 PyPI trusted publisher，见发布检查清单。

项目采用 [MIT](LICENSE)，源码位于 [yiminspace/ablation](https://github.com/yiminspace/ablation)。

## 外部日志关联

通过 `ablation config profile NAME --observers-json observers.json` 配置可选观察器；格式和完整示例见 [英文说明](README.md#optional-external-log-tracking)。平台名称只是标签，可同时关联代理和模型服务，无配置时不注入 header、不查询外部 API。

每次运行保存实验、批次和运行标识。观察器可配置追踪 header、响应 JSON Pointer、响应 header、查看链接模板，或独立日志 API 的精确匹配查询。日志查询使用单独的地址与环境变量凭证。请求追踪 ID 用 `request_id_header` / `request_id_pointer` 单独保存，不当作日志 ID；配置了 `lookup` 时，无论响应是否带 ID 都执行精确查询，以唯一匹配结果生成详情链接。GUI 的运行记录和详情展示单条日志详情与辅助日志列表链接，CLI 和导出文件保留相同证据。

日志延迟入库时，可为 `lookup` 配置 `attempts`（1–5，默认 1）和 `interval_seconds`（0–5，默认 1）。仅未命中精确记录时重试；多条匹配或请求失败立即停止，每次查询仍有 5 秒超时。最终无法关联会明确记录失败，不改变模型回放结果。

`available` 表示从配置的响应或唯一匹配的日志记录取得 ID 或链接；`linked` 只是生成查看入口，未确认日志存在；`unavailable` 表示关联失败。关联失败不改变模型请求结果或断言评分。旧实验无需迁移，历史记录不追补或改写。
