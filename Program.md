## HomeAgent Program

本文档只写**当前实现方案**，用于代码审阅与后续开发对齐；`README.md` 主要保留理论定义、设计动机与形式化描述。

当前实现链路可以概括为：`automations.yaml -> entities/channels -> zones/TCAE -> RAG -> normal_config -> USTG -> iterative_refinement_plan -> resolution_rules -> runtime_graph_walk`。
其中，从方案口径上看，**实体常态配置、非预期状态转换图静态生成、情境迭代细化**应视为同一个阶段；处理策略生成是其后的独立阶段。

### 公共约定

`src/common.py` 是所有脚本共用的基础库，本身不产出业务结果文件。它负责读取 `configurations/config.json`、解析 `.env`、统一解析路径、读写 JSON/YAML、加载 `core.entity_registry`、构造规则 UID，以及调用 OpenAI 兼容接口。所有脚本的输入输出路径都通过它的 `get_input_path()` / `get_output_path()` 解析，默认输出目录是 `src/configurations/home/`。

### Script 1：`1_extract_devices_and_bind_channels.py`

**输入**：`configurations/automations.yaml`、`configurations/config.json`、可选的 `configurations/core.entity_registry`、以及 AI 所需的 `.env`。  
**输出**：`home/devices.json` 与 `home/channels.json`。  
**工作流**：脚本先把 `automations.yaml` 规范化为规则列表，然后从 `trigger`、`condition`、`action` 中递归提取所有出现过的实体；为每个实体记录 `entity_id`、`domain`、出现位置、所属规则、原始上下文片段，并按出现位置给出结构角色（只观测、只执行、混合）。随后把实体列表和压缩后的规则上下文发给 AI 做 channel 绑定：传感器侧生成 `observes`，执行器侧优先生成按规则作用域区分的 `effects_by_rule`，并允许提出 `proposed_channels` 但不会自动采用。AI 返回结果后，脚本会做严格校验与归一化，只接受 `config.json.channels` 中定义过的 channel；若 AI 不可用或校验失败，则退化为保守空绑定。最终 `devices.json` 保存“规则里有哪些实体、各自出现在哪里”，`channels.json` 保存“这些实体观测/影响哪些物理通道”。

### Script 2：`2_bind_zones_and_build_tcae.py`

**输入**：`home/devices.json`、`home/channels.json`、`configurations/automations.yaml`、可选的 `core.entity_registry`。  
**输出**：`home/zones.json` 与 `home/tcae.json`。  
**工作流**：脚本先筛出在 Step 1 中已经绑定到物理通道的实体，然后进入交互式 zone 绑定流程；用户为传感器填写观测区域，为执行器填写源区域与可达区域，结果保存为 `zones.json`。接着脚本把 Home Assistant 的 trigger/condition/action 规范化为平台无关的结构表示：状态触发、数值触发、时间触发会分别转成统一节点；动作会抽取 `service`、`operation`、`target_entity` 与抽象后态 `post`。在此基础上，脚本把数值 trigger/condition 映射为环境引用 `E_T` / `E_C`，把执行动作结合 channel 绑定与 zone 绑定映射为环境效应 `E_A`，最后按规则汇总成完整的 TCAE 记录。`tcae.json` 是后续所有图分析的统一输入。

### Script 3：`3_build_rule_association_graph.py`

**输入**：`home/tcae.json`。  
**输出**：`home/rule_association_graph.json`，可选图片 `images/rule_association_graph.png`。  
**工作流**：脚本读取 TCAE 规则集后，为每条规则建立触发器节点、条件节点、动作节点，并补上规则内部流边；随后根据 `E_T`、`E_C`、`E_A` 引入环境节点。之后分三步加边：先加动作到环境、环境到动作的环境相关边，再加环境到触发器/条件的间接关联边，最后遍历规则对，判断动作是否能直接满足别的规则触发器、允许/禁用其条件，或在同一实体上形成动作关联。构图结束后会去重、编号、汇总统计，并在边元数据中补入实体显示名，最终得到组件级的规则关联图 RAG。

### Script 4：`4_generate_normal_config.py`

**输入**：`home/devices.json`、`home/channels.json`、`home/zones.json`、`home/tcae.json`、`config.json`、`.env`。  
**输出**：`home/normal_config.json`。  
**工作流**：脚本默认只把在 TCAE 中出现在动作目标侧的实体当作候选常态实体，因为这些实体才会被后续静态剪枝直接比较；也可以通过参数改成把所有实体都送给 AI。它会把实体的显示名、domain、规则中的可能后态、zone 绑定、channel 绑定、相关规则上下文组织成结构化上下文发给 AI，请 AI 提议哪些实体需要常态配置、对应的 `normal_values` 是什么。AI 结果会被严格校验，只保留候选实体中的合法条目。之后脚本提供交互式人工审核：可以逐条保留、编辑、删除，也可以手工补充新的常态实体；若已有 `normal_config.json`，还可以复用或覆盖合并。最终输出的核心内容是 `normal_entities[entity_id].normal_values`，供 Step 5 静态比较动作后态是否偏离常态。

### Script 5：`5_build_unexpected_state_transition_graph.py`

**输入**：`home/tcae.json`、`home/rule_association_graph.json`、`home/normal_config.json`。  
**输出**：`home/unexpected_state_transition_graph.json`，可选图片 `images/unexpected_state_transition_graph.png`。  
**工作流**：脚本先把 `normal_config.json` 归一化成可比较的常态值集合，再从 `tcae.json` 中抽取每个动作节点的目标实体与动作后态。随后，它把“终点动作是否作用于常态实体”作为保留标准：一类是后态本身偏离常态的弱非预期动作，另一类是虽然朝向常态、但后续可能被上游关联阻断的恢复动作。对这些终点动作，脚本会在 RAG 上做有界逆向 DFS，收集所有以它们为终点、且至少包含一条关联边的路径；如果配置了 `--positive-only`，则只保留正极性路径。最后，把这些路径涉及到的节点和边取出，并补全相应规则的内部流边，生成 USTG，同时记录路径级 `unexpected_paths` 与终点动作/后态注释，供下一步做情境细化。

### Script 6：`6_iterative_refinement_plan.py`

**输入**：`configurations/automations.yaml`、`home/devices.json`、`home/channels.json`、`home/zones.json`、`home/normal_config.json`；脚本内部会反复调用 Step 2、3、5 重建 TCAE、RAG、USTG。  
**输出**：`home/iterative_refinement_plan.json`，以及 `home/iterations/itX/` 下每轮的 `automations-itX.yaml`、`tcae-itX.json`、`rule_association_graph-itX.json`、`unexpected_state_transition_graph-itX.json`。  
**工作流**：这是“非预期状态转换图生成与迭代细化”的核心实现。脚本先基于当前 `automations.yaml` 生成 `it0` 的 TCAE/RAG/USTG，再从 USTG 中抽取终点局部规则关联候选。对每个候选，它会构造与该候选相关的实体、环境节点、常态配置与谓词池，请 AI 发现危险边界情境；随后再做一轮全局情境审查，检查这些情境是否足够区分“正常/危险/待确认”。若仍存在缺口，则请 AI 生成两类规则更新：`rule_completion`（新增规则）或 `rule_modification`（修改已有规则），并且程序端会再次校验这些更新是否只复用现有实体、目标规则是否存在。通过校验的更新会被应用到新的 `automations-it(X+1).yaml` 中，然后重新生成下一轮图；迭代直到没有新的有效更新，或者达到迭代上限。最终 `iterative_refinement_plan.json` 保存每轮候选、情境、规则更新、接受/拒绝结果与停止原因。

### Script 7：`7_generate_resolution_rules.py`

**输入**：`home/devices.json`、`home/channels.json`、`home/zones.json`、`home/tcae.json`、`home/normal_config.json`、`home/unexpected_state_transition_graph.json`、可选的 `home/iterative_refinement_plan.json`。  
**输出**：`home/resolution_rules.json`。  
**工作流**：脚本首先从 USTG 的 `unexpected_paths` 中抽取“终点局部规则关联候选”，只保留真正指向终点风险动作的直接/间接关联。然后把这些候选与 Step 6 的细化报告对齐：如果某个候选已经被规则补全充分覆盖，且没有 `remaining_context_gaps`，则策略默认是 `default`；否则脚本会把源规则、目标规则、候选关联证据、相关实体上下文、残余情境和固定策略模板发给 AI，请它在既定模板中选择一个运行时处理策略。AI 输出会被归一化成统一 policy 结构；若 AI 不可用，则退回需要人工复核的 `fallback_policy`。最终 `resolution_rules.json` 记录每个候选的 candidate 信息、对应的策略、是否用了迭代细化报告、残余风险与策略统计。

### Script 8：`8_runtime_graph_walk.py`

**输入**：`home/tcae.json`、`home/unexpected_state_transition_graph.json`、`home/resolution_rules.json`、`home/channels.json`、`home/zones.json`，以及 Home Assistant 在运行时通过 socket 发送的检查事件；监听地址来自 `.env` 中的 `HomeAgent_IP` 与 `HomeAgent_PORT`。  
**输出**：运行时会持续维护 `home/runtime_store.json` 与 `home/runtime_events.jsonl`，同时把干预命令回传给 Home Assistant。  
**工作流**：脚本启动一个本地运行时服务器，只处理两个检查点事件：`before_condition` 和 `after_condition`。收到事件后，它不会遍历整张长路径，而是只从当前节点向上找最近的局部路径：`A -> 当前节点` 或 `A -> E -> 当前节点`。为了避免轮询，脚本使用惰性刷新：只有当前检查点真正涉及到的上游动作节点和环境节点，才会回查 Home Assistant 的真实实体状态，并据此判断动作/环境影响是否仍有效。若在 `before_condition` 发现触发关联或条件禁用关联，或在 `after_condition` 发现条件允许关联或动作关联，脚本就会从 `resolution_rules.json` 中找到最合适的候选策略，把它翻译成 `default / stop / cancel` 等命令并立即返回；若没有检测到局部非预期关联，则默认放行当前规则执行。整个过程会把节点激活状态、环境影响、历史动作和每次决策日志写入运行时存储。

### 当前代码审阅建议

如果后续按实现顺序审阅，建议依次看：`common.py -> 1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7 -> 8`。其中，最重要的三个衔接点是：Step 1 到 Step 2 的 `devices/channels -> zones/TCAE`，Step 5 到 Step 6 的 `USTG -> 情境细化与规则更新`，以及 Step 7 到 Step 8 的 `resolution_rules -> 运行时命令执行`。这三处最容易出现“设计一致但实现口径不一致”的问题。