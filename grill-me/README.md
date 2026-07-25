# Grill Me (Qoder Plugin)

一场无情的逐题拷问，帮你压力测试计划、决策或想法，直到达成共识后才动手。

## 来源（Provenance）

- 原作者：Matt Pocock
- 源仓库：https://github.com/mattpocock/skills （skills.sh 上 65 万+ 安装的 `grill-me`）
- 源文件：
  - `skills/productivity/grill-me/SKILL.md`（入口命令，仅一行 "Run a /grilling session"）
  - `skills/productivity/grilling/SKILL.md`（实际拷问指令）

打包时将两者合并为单个 `grill-me` skill：入口文件无独立逻辑，合并后行为与源版本一致，并补充了中文触发词说明。

## 规范归属与同步方向（Canonical Source）

本 skill 在仓库中存在两份 `SKILL.md`，各自角色如下：

- `grill-me/skills/grill-me/SKILL.md` — **规范源（source of truth）**：插件分发源码，`grill-me/` 目录整体是可分发的 Qoder 插件包。
- `.qoder/skills/grill-me/SKILL.md` — **当前生效副本**：Qoder 在本工作区实际加载并生效的是这一份。

同步方向：**以 `grill-me/skills/grill-me/SKILL.md` 为准，单向同步到 `.qoder/skills/grill-me/`**。修改 skill 内容时先改插件源码这一份，再将其复制覆盖到 `.qoder/skills/grill-me/SKILL.md` 使改动生效；不要直接编辑 `.qoder/` 下的副本，避免两份内容漂移。可用以下命令同步并校验：

```sh
cp grill-me/skills/grill-me/SKILL.md .qoder/skills/grill-me/SKILL.md
diff grill-me/skills/grill-me/SKILL.md .qoder/skills/grill-me/SKILL.md  # 无输出即一致
```

## 包含内容

- `skills/grill-me/SKILL.md` — 拷问式访谈 skill
- `assets/avatar.svg` — 本地生成的插件图标（非官方 logo）

## 省略内容

- 源仓库中的 `agents/openai.yaml`（OpenAI 平台专用的展示元数据，Qoder 不需要）
- `batch-grill-me`（源仓库标记为 in-progress，且为独立工作流）

## 使用方式

对话中说 "grill me"、"/grill-me"、"拷问我" 或"压力测试一下我的想法"即可触发。
Agent 会一次只问一个问题（附推荐答案），逐层走完决策树，达成共识前不会动手实现。
