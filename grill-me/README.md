# Grill Me (Qoder Plugin)

一场无情的逐题拷问，帮你压力测试计划、决策或想法，直到达成共识后才动手。

## 来源（Provenance）

- 原作者：Matt Pocock
- 源仓库：https://github.com/mattpocock/skills （skills.sh 上 65 万+ 安装的 `grill-me`）
- 源文件：
  - `skills/productivity/grill-me/SKILL.md`（入口命令，仅一行 "Run a /grilling session"）
  - `skills/productivity/grilling/SKILL.md`（实际拷问指令）

打包时将两者合并为单个 `grill-me` skill：入口文件无独立逻辑，合并后行为与源版本一致，并补充了中文触发词说明。

## 包含内容

- `skills/grill-me/SKILL.md` — 拷问式访谈 skill
- `assets/avatar.svg` — 本地生成的插件图标（非官方 logo）

## 省略内容

- 源仓库中的 `agents/openai.yaml`（OpenAI 平台专用的展示元数据，Qoder 不需要）
- `batch-grill-me`（源仓库标记为 in-progress，且为独立工作流）

## 使用方式

对话中说 "grill me"、"/grill-me"、"拷问我" 或"压力测试一下我的想法"即可触发。
Agent 会一次只问一个问题（附推荐答案），逐层走完决策树，达成共识前不会动手实现。
