# Claude Memory

为 [Claude Code](https://docs.anthropic.com/en/docs/claude-code) 打造的持久化 AI 记忆系统。自动从每次会话中提取知识，以树状路径结构组织，基于使用频率智能遗忘与再激活。

## 功能特性

- **树状结构**: 记忆按 2~3 层路径组织（如 `flink/deploy/jar-mapping`），自动聚合同主题知识
- **自动提取**: 会话结束时调用 Claude Sonnet 从对话记录中提取关键知识，感知已有路径避免重复
- **自动加载**: 新会话开始时以树形格式展示所有活跃记忆，Claude 可直接引用历史知识
- **智能合并**: 基于路径前缀匹配检测相似条目，自动合并而非重复存储
- **频率遗忘**: 记忆超过 500KB 时，按评分公式 `score = 使用次数 × 0.6 + 时效性 × 0.4` 淘汰低分条目
- **自动再激活**: 已遗忘的记忆如果与新会话内容相似，自动从归档中恢复，保留历史使用次数
- **批量整合**: `consolidate` 命令可将扁平条目批量迁移为树状结构（191 条 → 25 条，212KB → 35KB）
- **零依赖**: 纯 Python 标准库实现，无需 pip install

## 工作原理

```
新会话开始 ──→ 加载活跃记忆（树形展示）
                    │
会话结束 ────→ 读取对话记录 (transcript)
                    │
              调用 Sonnet API 提取结构化知识（输出 path 格式）
                    │
              基于路径前缀匹配与 global_memory 合并去重
                    │
              扫描 deep_memory 中的相似条目 → 再激活
                    │
              总大小 > 500KB? → 淘汰低分条目 → deep_memory.json
```

### 评分机制

| 因子 | 权重 | 说明 |
|------|------|------|
| 使用次数 (access_count) | 60% | 该记忆被加载/引用的次数越多，分数越高 |
| 时效性 (recency) | 40% | 基于最后访问时间，180 天内线性衰减至 0 |

## 安装

```bash
git clone git@github.com:heartInGod/claude-memory.git
cd claude-memory
bash install.sh
```

安装脚本会自动完成：

1. 检查依赖 (python3, jq)
2. 复制 skill 文件到 `~/.claude/skills/claude-memory/`
3. 初始化数据文件（重装时保留已有数据）
4. 在 `~/.claude/settings.json` 中注入 `SessionStart` 和 `Stop` hooks
5. 验证安装

安装后重启 Claude Code 即可生效。

## 卸载

```bash
cd claude-memory
bash uninstall.sh
```

卸载时会询问是否保留记忆数据。

## 使用方式

### 自动模式（默认）

安装后全自动运行，无需手动操作：

- **新会话**: 记忆自动加载为上下文，Claude 可引用过往知识
- **会话结束**: 重要信息自动提取并存储

### 手动命令

```bash
# 查看记忆统计
python3 ~/.claude/skills/claude-memory/scripts/memory_manager.py stats

# 搜索记忆（包括已遗忘的）
python3 ~/.claude/skills/claude-memory/scripts/memory_manager.py recall --query "关键词"

# 强制执行遗忘清理
python3 ~/.claude/skills/claude-memory/scripts/memory_manager.py forget

# 从归档中恢复指定记忆
python3 ~/.claude/skills/claude-memory/scripts/memory_manager.py reactivate --id "mem_20260527_001_abc1"

# 批量整合：将扁平条目迁移为树状路径结构
python3 ~/.claude/skills/claude-memory/scripts/memory_manager.py consolidate --dry-run  # 预览
python3 ~/.claude/skills/claude-memory/scripts/memory_manager.py consolidate            # 执行

# 手动从对话记录中提取
python3 ~/.claude/skills/claude-memory/scripts/memory_manager.py extract \
    --transcript /path/to/transcript.jsonl \
    --session-id "可选的会话ID"
```

### 在 Claude Code 中使用

安装后 claude-memory 会注册为 Claude Code skill，可以直接对 Claude 说：

- "查看记忆统计"
- "搜索我的记忆：canoe"
- "有哪些被遗忘的记忆？"

## 记忆分类

记忆通过路径层级自然归类，无需显式 category 字段：

| 路径示例 | 说明 |
|----------|------|
| `flink/deploy/jar-mapping` | Flink 部署相关知识 |
| `feishu/auth` | 飞书认证配置 |
| `github/ssh` | GitHub SSH 连接方案 |
| `canoe/troubleshoot` | Canoe 排障流程 |

## 数据文件

| 文件 | 路径 | 说明 |
|------|------|------|
| `global_memory.json` | `~/.claude/skills/claude-memory/data/` | 活跃记忆，每次会话加载 |
| `deep_memory.json` | `~/.claude/skills/claude-memory/data/` | 遗忘归档，可搜索、可恢复 |

### 条目格式

```json
{
  "id": "mem_20260527_112241_6bcc",
  "path": "flink/deploy/jar-mapping",
  "content": "核心知识内容，简洁但完整",
  "created_at": "2026-05-27T11:22:41+00:00",
  "last_accessed": "2026-05-27T11:22:41+00:00",
  "access_count": 3,
  "importance": "high"
}
```

## Hooks 配置

安装脚本会在 `~/.claude/settings.json` 中添加：

```json
{
  "hooks": {
    "SessionStart": [{
      "hooks": [{
        "type": "command",
        "command": "bash ~/.claude/skills/claude-memory/scripts/session_start.sh",
        "timeout": 30
      }]
    }],
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "bash ~/.claude/skills/claude-memory/scripts/session_stop.sh",
        "timeout": 120
      }]
    }]
  }
}
```

## 环境要求

- Python 3.8+
- jq
- Claude Code（需支持 hooks）
- Anthropic API 访问权限（用于 Sonnet 提取调用）

## 项目结构

```
claude-memory/
├── install.sh                       # 一键安装脚本
├── uninstall.sh                     # 卸载脚本（可选保留数据）
├── README.md
└── skill/
    ├── SKILL.md                     # Claude Code skill 定义
    ├── scripts/
    │   ├── memory_manager.py        # 核心逻辑（提取/合并/遗忘/加载/搜索）
    │   ├── session_start.sh         # SessionStart hook
    │   └── session_stop.sh          # Stop hook
    └── data/
        └── .gitkeep
```

## License

MIT
