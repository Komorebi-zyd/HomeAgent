# HA_Replacement

用于替换 Home Assistant 源码中的少量文件，在自动化执行链路中插入 **运行时通信与干预钩子**，使 HA 可以把当前规则执行上下文发送给外部检测器，并接收冲突处理命令。

---

## 1. 替换的文件

- `homeassistant/components/automation/__init__.py`
- `homeassistant/helpers/script.py`
- `homeassistant/zyd/communicate.py`

---

## 2. 改动总览

### 2.1 `automation/__init__.py`
在 `AutomationEntity.async_trigger(...)` 中插入 **before_condition** 钩子：

- 位置：变量渲染完成后、条件判断前
- 作用：
  - 上报当前触发的自动化规则
  - 允许外部检测器在条件判断前返回控制命令
  - 可实现提前终止、跳过条件、强制执行其他规则、回滚状态

### 2.2 `helpers/script.py`
在 `Script.async_run(...)` 中插入 **after_condition** 钩子：

- 位置：条件已通过、动作真正执行前
- 作用：
  - 上报即将执行动作的规则
  - 允许外部检测器在动作落地前返回控制命令
  - 可实现动作前仲裁与干预

### 2.3 `zyd/communicate.py`
新增 HA 侧 socket 客户端通信模块：

- 负责连接外部检测器
- 根据外部请求返回 HA 当前实体信息
- 收集并返回外部下发的命令列表

---

## 3. 执行流程

### 3.1 before_condition 阶段
触发入口：`AutomationEntity.async_trigger(...)`

流程：
1. 当前自动化被触发
2. 生成 `variables`
3. 调用 `zyd_communicate({"location": "before_condition", "entity_id": 当前规则}, 实体列表, 实体状态)`
4. 执行外部返回命令
5. 再决定是否继续 condition 判断

### 3.2 after_condition 阶段
触发入口：`Script.async_run(...)`

流程：
1. 当前规则 condition 已满足
2. 即将进入 action 执行
3. 调用 `zyd_communicate({"location": "after_condition", "entity_id": 当前规则}, 实体列表, 实体状态)`
4. 执行外部返回命令
5. 再决定是否继续 action 执行

### 3.3 递归保护
通过变量 `zyd_execute=True` 标记“内部强制执行的子规则”：

- 若检测到 `zyd_execute=True`
- 则不再进入 socket 通信流程
- 避免递归检测与重复通信

---

## 4. 命令协议

外部检测器可返回以下命令：

### `default`
- 含义：不干预，按 HA 原流程继续

### `run`
- 含义：强制执行指定规则
- 格式：
  - `{"type": "run", "entity_id": "automation.xxx"}`

### `cancel`
- 含义：回滚指定实体状态
- 格式：
  - `{"type": "cancel", "entity_id": "automation.xxx", "entities": {"entity.a": "off"}}`

### `start`
- 含义：强制当前规则继续执行
- 在 `before_condition` 中表现为：跳过条件检查

### `stop`
- 含义：终止当前规则
- 在 `before_condition` 中：直接返回，不再继续
- 在 `after_condition` 中：阻止动作执行

---

## 5. 关键函数说明

## 5.1 `AutomationEntity.async_trigger(self, run_variables, context=None, skip_condition=False)`

**所在文件**：`homeassistant/components/automation/__init__.py`

**描述**：
Home Assistant 自动化规则触发入口。此处被插入 **before_condition** 钩子，用于在条件判断前与外部检测器通信。

**输入参数**：
- `self`：当前自动化实体对象
- `run_variables: dict[str, Any]`
  - 当前规则的运行变量
  - 特别字段：
    - `trigger`：触发信息
    - `zyd_execute`：若为 `True`，表示该次触发为内部强制执行，不再通信
- `context: Context | None`
  - HA 运行上下文
- `skip_condition: bool`
  - 是否跳过条件判断

**输出参数**：
- `ScriptRunResult | None`
  - 正常执行时返回脚本运行结果
  - 若被 `stop` 干预或条件失败，返回 `None`

**插装后新增行为**：
- 调用 `zyd_communicate(...)`
- 根据返回命令执行：
  - `run`：强制运行其他规则
  - `cancel`：修改实体状态
  - `start`：设置 `skip_condition=True`
  - `stop`：终止当前规则

---

## 5.2 `get_rule_variable(self, entity_id)`

**所在位置**：`AutomationEntity.async_trigger(...)` 内部辅助函数

**描述**：
为“强制执行其他规则”构造运行变量。

**输入参数**：
- `self`：当前自动化实体对象
- `entity_id: str`：目标自动化规则实体 ID

**输出参数**：
- `dict`
  - 典型内容：
    - `this`：目标规则当前 state dict
    - `trigger`：`{"platform": None}`
    - `zyd_execute`：`True`

---

## 5.3 `run_rule(self, entity_id)`

**所在位置**：`AutomationEntity.async_trigger(...)` 内部辅助函数

**描述**：
强制触发指定自动化规则，并附加 `zyd_execute=True`，避免二次通信。

**输入参数**：
- `self`：当前自动化实体对象
- `entity_id: str`：目标自动化规则实体 ID

**输出参数**：
- 无显式返回值

---

## 5.4 `set_entity_state(self, entity_id, new_state)`

**所在位置**：`AutomationEntity.async_trigger(...)` / `Script.async_run(...)` 内部辅助函数

**描述**：
直接修改 HA 中指定实体状态，用于执行 `cancel` 命令时回滚状态。

**输入参数**：
- `self`：当前对象
- `entity_id: str`：实体 ID
- `new_state: Any`：目标状态

**输出参数**：
- 无显式返回值

---

## 5.5 `Script.async_run(self, run_variables=None, context=None, started_action=None)`

**所在文件**：`homeassistant/helpers/script.py`

**描述**：
HA 脚本运行入口。此处被插入 **after_condition** 钩子，用于在动作执行前与外部检测器通信。

**输入参数**：
- `self`：当前脚本对象
- `run_variables: dict | None`
  - 当前规则运行变量
  - 特别字段：
    - `this`：当前自动化实体状态
    - `zyd_execute`：若为 `True`，则不再通信
- `context: Context | None`
  - HA 运行上下文
- `started_action: Callable | None`
  - 动作开始时的回调

**输出参数**：
- `ScriptRunResult | None`
  - 正常执行返回脚本结果
  - 被 `stop` 干预时返回 `None`

**插装后新增行为**：
- 在动作执行前调用 `zyd_communicate(...)`
- 根据命令执行：
  - `run`：强制执行其他规则
  - `cancel`：回滚实体状态
  - `stop`：终止当前动作执行

---

## 5.6 `sock_recv(socket)`

**所在文件**：`homeassistant/zyd/communicate.py`

**描述**：
从 socket 接收 JSON 消息，并反序列化为 Python 对象。

**输入参数**：
- `socket`：已连接的 socket 对象

**输出参数**：
- `dict | None`
  - 接收到的消息字典
  - 若消息为空，返回 `None`

---

## 5.7 `sock_send(socket, message)`

**所在文件**：`homeassistant/zyd/communicate.py`

**描述**：
将 Python 字典编码为 JSON，并通过 socket 发送。

**输入参数**：
- `socket`：已连接的 socket 对象
- `message: dict`：待发送消息

**输出参数**：
- `None`

---

## 5.8 `zyd_communicate(now_entity, entities, entity_state)`

**所在文件**：`homeassistant/zyd/communicate.py`

**描述**：
HA 侧通信主函数。连接外部检测器，发送当前规则上下文，并响应外部的状态查询与命令下发。

**输入参数**：
- `now_entity: dict`
  - 当前执行规则信息
  - 格式示例：
    - `{"location": "before_condition", "entity_id": "automation.xxx"}`
    - `{"location": "after_condition", "entity_id": "automation.xxx"}`
- `entities: list`
  - 当前 HA 中全部实体 ID 列表
- `entity_state: dict`
  - HA 当前状态对象映射，一般为 `hass.states._states`

**输出参数**：
- `list[dict] | None`
  - 返回外部检测器下发的命令列表
  - 若连接失败，则返回 `None`

**内部支持的查询类型**：
- `type=0`：获取全部实体列表
- `type=1`：获取单个实体状态
- `type=2`：获取当前时间
- `type=3`：接收命令列表
- `type=-1`：结束通信

---

## 6. 可获取的信息

通过当前插装，外部检测器可以间接获取：

- 当前执行到哪个规则：`entity_id`
- 当前阶段：`before_condition / after_condition`
- HA 全部实体列表
- 任意实体当前状态 `state`
- 任意实体最近变更时间 `last_changed`
- 自动化最近触发时间 `last_triggered`
- 当前 UTC 时间

这些信息足够支持：
- 运行时冲突检测
- 条件/触发时序判断
- 动作前仲裁
- 状态回滚与规则强制执行

---

## 7. 备注

- `homeassistant/helpers/script.py` 中 `_async_step(...)` 位置仅留了 `zyd` 标记注释，主要功能改动实际落在 `Script.async_run(...)`。
- `homeassistant/HomeAssistant-zyd文档.docx` 为说明文档，不参与运行。
