## HomeGuard

### TODO

- [X] 理论定义完备
  - [X] 设备实体
  - [X] 环境因素
  - [X] 触发器
  - [X] 条件
  - [X] 执行动作
  - [X] 规则模型
  - [X] 规则关联
  - [X] 规则关联图
    - [X] 节点定义
    - [X] 边定义
  - [X] 规则关联非预期结果定义（即违反实体常态配置）
  - [X] 非预期状态转换图（规则关联图根据实体常态配置得到的结果）
- [ ] 方案设计【Agent+】
  - [X] zone+channel感知
  - [X] TCAE建模
  - [X] 规则关联图生成算法（静态代码分析）
  - [X] 非预期状态转换图生成算法（规则关联图根据实体常态配置得到的结果）
  - [X] 非预期状态处理策略生成算法（家庭环境上下文+产生关联的规则事件上下文+模板+AI判断）
  - [ ] 图游走算法
- [ ] 规则关联边定义与规则冲突实例设计（8+8）
  - [ ] 触发（直接/间接） $\rightarrow 2$
  - [ ] 条件（使允许/使禁用）（直接/间接） $\rightarrow 2\times 2=4$
  - [ ] 执行动作 $\rightarrow 2$
- [ ] 实验与优化（反哺设计）
  - [ ] 之前定义的判断标准是：规则交互/冲突类型是否检测到，但是不同方案设计并不相同，因此结果展示效果不好
- [ ] 其他实验比对
  - [ ] 冲突检测结果（设计的冲突是否检测出来）
  - [ ] 冲突应对效果
  - [ ] 性能

### 环境配置

> Home Assistant

- 自动化 / 脚本 / 场景
  - `automations.yaml`
  - `scripts.yaml`
  - `scenes.yaml`
- Helper（辅助实体/虚拟设备）
  - `.storage/input_boolean`
  - `.storage/input_button`
  - `.storage/input_datetime`
  - `.storage/input_number`
  - `.storage/input_select`

> Home Agent

```
HomeAgent/
├── README.md                                     //项目说明与理论定义
├── 关键词.md                                      //术语维护文件
│
└── src/
    ├── .env                                      //隐私配置，不读取、不提交
    ├── .env.example                              //隐私配置模板
    │
    ├── common.py                                 //公共工具：读配置、路径拼接、读写JSON/YAML、加载.env、调用AI
    │
    ├── 1_extract_devices_and_bind_channels.py    //提取规则实体，并用AI绑定设备影响的channel
    ├── 2_bind_zones_and_build_tcae.py            //用户输入zone，并生成TCAE规则模型
    ├── 3_build_rule_association_graph.py         //根据TCAE生成规则关联图
    ├── 4_generate_normal_config.py               //用AI生成实体常态配置
    ├── 5_build_unexpected_state_transition_graph.py //根据常态配置剪枝生成非预期状态转换图
    ├── 6_generate_resolution_rules.py            //根据常态、家庭环境、关联模式和模板生成处理规则
    │
    ├── configurations/
    │   ├── config.json                           //系统配置文件
    │   ├── automations.yaml                      //Home Assistant自动化规则
    │   ├── core.entity_registry                  //Home Assistant实体映射
    │   │
    │   └── home/
    │       ├── devices.json                      //脚本1输出：从规则中提取的实体信息
    │       ├── channels.json                     //脚本1输出：设备-channel绑定结果
    │       ├── zones.json                        //脚本2输出：设备-zone绑定结果
    │       ├── tcae.json                         //脚本2输出：TCAE规则模型
    │       ├── rule_association_graph.json       //脚本3输出：规则关联图
    │       ├── normal_config.json                //脚本4输出：实体常态配置
    │       ├── unexpected_state_transition_graph.json //脚本5输出：非预期状态转换图
    │       └── resolution_rules.json             //脚本6输出：非预期结果处理规则
    │
    └── images/
        ├── rule_association_graph.png            //脚本3输出：规则关联图图片
        └── unexpected_state_transition_graph.png //脚本5输出：非预期状态转换图图片
```

`.env`：

```
# 系统变量

HomeAssistant_IP=192.168.77.4
HomeAssistant_PORT=8123

HomeAgent_IP=192.168.77.254
HomeAgent_PORT=8081

USER = cst-zyd
PASS = zyd

AI_MODEL= deepseek-4-flash
AI_API_URL = https://api.deepseek.ai/v1
AI_API_KEY = sk-xxx
```

### 基础定义

$$
\text{TCAE规则模型}
\Rightarrow
\text{规则关联图}
\Rightarrow
\text{实体常态配置}
\Rightarrow
\text{非预期状态转换图}
\Rightarrow
\text{动态图游走验证与处理}
$$

#### 原子定义

##### 基本集合

- $\mathcal{Z}$：区域（zone）的有限集合；
- $\mathcal{C}$：物理通道（channel）的有限集合；
- $\mathcal{E}$：实体（entity）的有限集合;
  - 对于每个实体 $e \in \mathcal{E}$，定义其状态域为：$\mathcal{V}_e$，例如：
    - 开关类实体：$\mathcal{V}_e = \{\texttt{on}, \texttt{off}\}$；
    - 数值类实体：$\mathcal{V}_e \subseteq \mathbb{R}$。
- $\mathcal{E}_a \subseteq \mathcal{E}$：可执行动作的实体集合；
- $\mathcal{E}_s \subseteq \mathcal{E}$：可观测实体集合。
- $\mathbb{T}$：表示时间域，例如离散时间域或连续时间域。

##### 离散实体状态空间

- 整个系统的离散实体状态空间定义为：$\mathcal{X} = \prod_{e \in \mathcal{E}} \mathcal{V}_e$
- 任意时刻的离散实体状态记为：$x : \mathbb{T} \to \mathcal{X}$
  - $x(t)$ 是时刻 $t$ 的离散实体状态
  - $x(t)(e)$ 是实体 $e$ 在时刻 $t$ 的值

##### 环境状态空间

- 对每个通道 $c \in \mathcal{C}$，定义其值域：$\mathcal{D}_c \subseteq \mathbb{R}$
- 环境状态空间定义为：$\mathcal{Y} = \prod_{(z,c)\in \mathcal{Z}\times\mathcal{C}} \mathcal{D}_c$
- 任意时刻的环境状态记为：$y:\mathbb{T}\to\mathcal{Y}$
  - 其中 $y(t)$ 是时刻 $t$ 的环境状态
  - $y(t)(z,c)$ 是时刻 $t$ 区域 $z$ 中通道 $c$ 的值

##### 全局混合状态

- 系统在时刻 $t$ 的全局状态定义为：$\sigma(t) = (x(t), y(t))$
- 总状态空间为：$\Sigma = \mathcal{X} \times \mathcal{Y}$

##### 实体状态原子谓词

- 对于实体 $e \in \mathcal{E}$、关系符 $\bowtie \in \{=,\neq,<,\le,>,\ge\}$、值 $v \in \mathcal{V}_e$，定义实体状态原子谓词：$P_{e,\bowtie,v}(\sigma(t))\overset{\text{def}}{\Longleftrightarrow}x(t)(e)\;\bowtie v$
  - 例如 $P_{\texttt{doorlock},=,\texttt{unlocked}}(\sigma(t))$ 表示“门锁在时刻 $t$ 为 unlocked”。

##### 实体状态跃迁原子谓词

> 该谓词用于刻画状态型触发器。

- 对于实体 $e \in \mathcal{E}$、前后状态 $v_1,v_2 \in \mathcal{V}_e$，定义：$\Delta P_{e,v_1\to v_2}(\sigma(t^-),\sigma(t))\overset{\text{def}}{\Longleftrightarrow}x(t^-)(e)=v_1 \land x(t)(e)=v_2$

##### 环境状态原子谓词

对于区域 $z \in \mathcal{Z}$、通道 $c \in \mathcal{C}$、关系符 $\bowtie$、阈值 $\theta \in \mathcal{D}_c$，定义：

- $Q_{z,c,\bowtie,\theta}(\sigma(t))\overset{\text{def}}{\Longleftrightarrow}y(t)(z,c)\;\bowtie\;\theta$
  - 例如：$Q_{\texttt{bedroom},\texttt{temperature},\ge,30}(\sigma(t))$ 表示“卧室温度不低于 30”。
  - $\bowtie \in \{<,\le,>,\ge\}$

##### 环境阈值穿越原子谓词

> 为了刻画 numeric trigger，定义“阈值穿越事件”。

- **上穿阈值**: $\Delta Q^{\uparrow}_{z,c,\theta}(\sigma(t^-),\sigma(t))\overset{\text{def}}{\Longleftrightarrow}y(t^-)(z,c) < \theta \land y(t)(z,c) \ge \theta$
- **下穿阈值**: $\Delta Q^{\downarrow}_{z,c,\theta}(\sigma(t^-),\sigma(t))\overset{\text{def}}{\Longleftrightarrow}y(t^-)(z,c) > \theta \land y(t)(z,c) \le \theta$

##### 时间原子谓词

- 定义$\Omega$时间约束集合，且$\omega \in \Omega$
- 对任意时间约束 $\omega$，定义时间谓词：$H_{\omega}(t)$
  - 例如：
    - $H_{=19:00}(t)$
    - $H_{\in [19:00,23:00]}(t)$

#### TCAE 模型定义

##### Trigger定义

- 一条规则的 Trigger 作用于“状态变化事件”，定义为：$T_r : \Sigma \times \Sigma \times \mathbb{T} \to \{0,1\}$
  - 即：$T_r(\sigma(t^-), \sigma(t), t)$ 表示规则 $r$ 是否在时刻 $t$ 被触发。
- 若规则 $r$ 有 $m_r$ 个触发项，则定义：$T_r = \bigvee_{i=1}^{m_r} \varphi_{r,i}$
  - 其中每个 $\varphi_{r,i}$ 是由以下原子构成的布尔公式：
    - $\Delta P_{e,v_1\to v_2}$
    - $\Delta Q^{\uparrow}_{z,c,\theta}$
    - $\Delta Q^{\downarrow}_{z,c,\theta}$
    - $H_\omega$

##### Condition 的定义

- Condition 是对当前状态的约束，定义为：$C_r : \Sigma \times \mathbb{T} \to \{0,1\}$
  - 即：$C_r(\sigma(t), t)$
- 若规则 $r$ 有 $n_r$ 个条件项，则定义：$C_r = \bigwedge_{j=1}^{n_r} \psi_{r,j}$
  - 其中每个 $\psi_{r,j}$ 是由以下原子构成的布尔公式：
    - $P_{e,\bowtie,v}$
    - $Q_{z,c,\bowtie,\theta}$
    - $H_\omega$

##### Action 的定义

Action 不是谓词，而是状态变换。

- 定义 $\mathcal{A}$ 表示所有原子动作的集合
- 每个原子动作可以抽象为：$a=(e,\textsf{op},v)$其中：

  - $e\in\mathcal{E}_a$
  - $\textsf{op}$ 为动作类型
  - $v$ 为目标值或参数
- 设规则 $r$ 的动作序列为：$A_r = \langle a_{r,1}, a_{r,2}, \dots, a_{r,\ell_r} \rangle$
- 每个动作 $a$ 形式化为一个部分状态变换：$\delta_a : \mathcal{X} \to \mathcal{X}$

  - 例如，若动作 $a=(e,\textsf{set},v)$ 表示将实体 $e$ 设置为值 $v$，则其变换定义为：

  $$
  \delta_a(x)(u)=
      \begin{cases}
      v, & u=e \\
      x(u), & u\neq e
      \end{cases}
  $$
- 规则 $r$ 的动作序列对应的整体离散状态变换为：$\delta_{A_r}=\delta_{a_{r,\ell_r}}\circ\delta_{a_{r,\ell_r-1}}\circ \cdots \circ\delta_{a_{r,1}}$

##### Environment 的定义

这里把 Environment 分成三部分：

- $E_r^T$：Trigger 对环境的引用；
- $E_r^C$：Condition 对环境的引用；
- $E_r^A$：Action 对环境的效应。

###### 环境引用项

- 定义环境引用原子集合 $\mathcal{R}_{env}$，每个环境引用项写为：$\rho = (z,c,\bowtie,\theta)$，$\bowtie \in \{<,\le,>,\ge,\uparrow,\downarrow\}$
  - 其中状态型引用有：$\bowtie\in\{<,\le,>,\ge\}$
  - 事件型引用有：$\bowtie\in\{\uparrow,\downarrow\}$
- 其语义定义为：$\llbracket \rho \rrbracket (\sigma(t))\overset{\text{def}}{\Longleftrightarrow}Q_{z,c,\bowtie,\theta}(\sigma(t))$
- 若是 trigger 中的阈值穿越项，则可写为：
  - 上穿型引用: $\rho = (z,c,\uparrow,\theta), \qquad \llbracket \rho \rrbracket(\sigma(t^-),\sigma(t)) \overset{\text{def}}{\Longleftrightarrow}\Delta Q^{\uparrow}_{z,c,\theta}(\sigma(t^-),\sigma(t))$
  - 下穿型引用: $\rho = (z,c,\downarrow,\theta), \qquad \llbracket \rho \rrbracket(\sigma(t^-),\sigma(t)) \overset{\text{def}}{\Longleftrightarrow} \Delta Q^{\downarrow}_{z,c,\theta}(\sigma(t^-),\sigma(t))$
- 环境引用项集合：$\operatorname{RefEnv}(F)=\{\rho\in\mathcal{R}_{env}\mid \rho \text{ appears in } F\}$，表示公式 $F$ 中出现的全部环境引用项的集合。

###### 环境效应项

- 定义**环境效应原子集合** $\mathcal{F}_{env}$。每个环境效应项定义为：$\eta = (z_s,\mathcal{R},c,s,\mu,\lambda,d)$，$s \in \{+1,-1\}$
  - 其中：
    - $z_s \in \mathcal{Z}$：源区域；
    - $\mathcal{R} \subseteq \mathcal{Z}$：该效应可传播到的区域集合；
    - $c \in \mathcal{C}$：受影响的通道；
    - $s \in \{+1,-1\}$：变化方向；
    - $\mu \in \mathbb{R}_{\ge 0}$：最小影响强度；
    - $\lambda \in \mathbb{R}_{\ge 0}$：生效延迟；
    - $d \in \mathbb{R}_{>0}$：持续时间。
- 若动作于时刻 $t$ 执行，并产生环境效应: $\eta = (z_s,\mathcal{R},c,s,\mu,\lambda,d)$
- 语义定义为: $\forall z \in \mathcal{R},\exists t' \in [t+\lambda,\; t+\lambda+d]\text{ s.t. }s\cdot\bigl(y(t')(z,c)-y(t)(z,c)\bigr)\ge \mu$，该定义刻画最弱环境可达影响
  - 表示：
    - 若 $s=+1$，则该通道值在传播窗口内至少上升 $\mu$；
    - 若 $s=-1$，则该通道值在传播窗口内至少下降 $\mu$。
- **动作到环境效应的映射**
  - 定义环境模型函数：$\Gamma : \mathcal{A} \to 2^{\mathcal{F}_{env}}$
  - 其中 $\mathcal{A}$ 为所有可能动作的集合，规则 $r$ 的**动作环境效应集合**定义为：$E_r^A = \bigcup_{a\in A_r}\Gamma(a)$

##### TCAE 模型

- 一条规则 $r$ 定义为：$r = \langle T_r,\; C_r,\; A_r,\; E_r \rangle$
  - 其中 $E_r = (E_r^T,\; E_r^C,\; E_r^A)$
    - Trigger 的环境引用集: $E_r^T = \operatorname{RefEnv}(T_r)$
    - Condition 的环境引用集: $E_r^C = \operatorname{RefEnv}(C_r)$
    - Action 的环境效应集: $E_r^A = \bigcup_{a\in A_r}\Gamma(a)$
- 完整的 TCAE 规则如下：$\boxed{r = \langle T_r,\; C_r,\; A_r,\; (E_r^T,E_r^C,E_r^A)\rangle}$
- 规则 $r$ 在时刻 $t$ 被触发，当且仅当: $\operatorname{Trig}(r,t)\overset{\text{def}}{\Longleftrightarrow}T_r(\sigma(t^-),\sigma(t),t)=1$
- 规则 $r$ 在时刻 $t$ 条件满足，当且仅当: $\operatorname{Cond}(r,t)\overset{\text{def}}{\Longleftrightarrow}C_r(\sigma(t),t)=1$
- 规则 $r$ 在时刻 $t$ 执行
  - 当且仅当: $\operatorname{Fire}(r,t)\overset{\text{def}}{\Longleftrightarrow}\operatorname{Trig}(r,t)\land \operatorname{Cond}(r,t)$
  - 执行后，其离散实体状态更新为: $x(t^+) = \delta_{A_r}(x(t))$
  - 其环境状态则在后续时间区间内满足环境效应约束。记为：$y_r(t \rightsquigarrow t') \models E_r^A$
    - $y_r(t\rightsquigarrow t')$ 表示规则 $r$ 在 $[t,t']$ 上诱导的环境轨迹；
    - 符号 $\models$ 表示该环境轨迹满足环境效应集合 $E_r^A$ 中每个效应项的约束。

##### 环境摘要

> 为了后续定义规则交互公式，我们还需要把环境引用与环境效应统一映射成可比较的“摘要”。

- 环境引用摘要（来自 Trigger / Condition 对环境的依赖）

  - 对环境引用项 $\rho$，定义其摘要函数: $\operatorname{sum}(\rho) = (z,c,\operatorname{dir}(\rho))$
  - 其中方向函数定义为：

  $$
  \operatorname{dir}(z,c,\ge,\theta)=+1,\qquad
  \operatorname{dir}(z,c,>,\theta)=+1
  $$

  $$
  \operatorname{dir}(z,c,\le,\theta)=-1,\qquad
  \operatorname{dir}(z,c,<,\theta)=-1
  $$

  $$
  \operatorname{dir}(z,c,\uparrow,\theta)=+1,\qquad
  \operatorname{dir}(z,c,\downarrow,\theta)=-1
  $$
- Trigger 的环境引用摘要: $\widehat{E}_r^T=\{\operatorname{sum}(\rho)\mid \rho\in E_r^T\}$
- Condition 的环境引用摘要: $\widehat{E}_r^C=\{\operatorname{sum}(\rho)\mid \rho\in E_r^C\}$
- 环境效应摘要（来自 Action 对环境的影响）

  - 对环境效应项$\eta=(z_s,\mathcal{R},c,s,\mu,\lambda,d)$，定义其对目标区域 $z\in\mathcal{R}$ 的投影摘要为：$\operatorname{proj}(\eta,z)=(z,c,s)$
  - 定义规则 $r$ 的动作环境摘要集合: $\widehat{E}_r^A=\bigcup_{\eta=(z_s,\mathcal{R},c,s,\mu,\lambda,d)\in E_r^A}\{\operatorname{proj}(\eta,z)\mid z\in\mathcal{R}\}$

#### 规则关联图相关定义

##### 节点定义

> 节点类型包括“触发器节点”、“条件节点”、“动作节点”与“环境节点”

- 设规则集合为：$\mathcal{R}=\{r_1,r_2,\dots,r_n\}$
- 每条规则仍采用 TCAE 表示：$r=\langle T_r,C_r,A_r,(E_r^T,E_r^C,E_r^A)\rangle$
- 由于规则间的关联的多样性，因此将规则 $r$ 拆分为组件节点
- 规则关联图的节点集合定义为：$V=V^T\cup V^C\cup V^A\cup V^E$

###### 触发器节点

- 对每条规则 $r$，定义其触发器节点为：$v_r^T$，表示规则 $r$ 的触发器部分 $T_r$
- 触发器节点集合为：$V^T=\{v_r^T\mid r\in\mathcal{R}\}$

###### 条件节点

- 若规则 $r$ 存在非空条件 $C_r$，定义其条件节点为：$v_r^C$
- 条件节点集合为：$V^C=\{v_r^C\mid r\in\mathcal{R},\ C_r\neq \emptyset\}$
- 其中 $C_r\eq \emptyset$ 表示规则没有条件

###### 动作节点

- 对每条规则 $r$，定义其动作节点为：$v_r^A$
- 表示规则 $r$ 的执行动作序列 $A_r$。
- 动作节点集合为：$V^A=\{v_r^A\mid r\in\mathcal{R}\}$

###### 环境节点

> 环境参数节点不属于某条规则，而属于系统环境。

- 对任意区域 $z\in\mathcal{Z}$ 和通道 $c\in\mathcal{C}$，定义环境参数节点：
  - $v_{z,c}^E$，表示区域 $z$ 中的物理通道 $c$。
- 环境参数节点集合为：$V^E=\{v_{z,c}^E\mid z\in\mathcal{Z},\ c\in\mathcal{C},\ (z,c)\text{ appears in some }E_r^T,E_r^C,E_r^A\}$

##### 边定义

> 规则关联图中的边分为两类：
>
> - **规则内部边**：规则自身执行流程带来的边；
> - **规则关联边**：不同规则组件或环境参数之间的潜在影响关系。
> - 因此边集合定义为：$E=E^{intra}\cup E^{assoc}$

###### 规则内部边

> 规则内部边表示规则自身的控制流，不代表规则之间的关联。

- 有条件规则:
  - 若规则 $r$ 有显式条件，即 $C_r\neq \emptyset$，则加入两条内部边：$(v_r^T,v_r^C)\in E^{intra}$、$(v_r^C,v_r^A)\in E^{intra}$
  - 表示：$T_r \rightarrow C_r \rightarrow A_r$，即触发器满足后检查条件，条件满足后执行动作。
- 无条件规则:
  - 若规则 $r$ 没有显式条件，即 $C_r=\emptyset$，则加入一条内部边：$(v_r^T,v_r^A)\in E^{intra}$
  - 表示：$T_r \rightarrow A_r$，即触发器满足后直接执行动作。

###### 规则关联边

> 规则关联边表示某个规则的动作可能影响其他规则的触发器、条件、动作，或者影响环境参数。

- 所有关联边都带有一个方向标签：$\operatorname{pol}(e)\in\{+1,-1\}$，其中：
  - $\operatorname{pol}(e)=+1$ 表示促进、满足、增强、趋向同向；
  - $\operatorname{pol}(e)=-1$ 表示抑制、破坏、削弱、趋向反向。
- 规则内部边可视为中性传播边，其极性为：$\operatorname{pol}(e)=+1$，该极性只用于路径极性计算，不表示规则内部边属于规则关联边。

###### 规则关联边类型

> 规则关联边按照影响方式分为九类，命名如下：

$$
E^{assoc}
=

E^{DT}
\cup E^{ET}
\cup E^{DCA}
\cup E^{DCD}
\cup E^{ICA}
\cup E^{ICD}
\cup E^{DA}
\cup E^{IA}
\cup E^{AE}
$$

| 符号        | 名称               | 方向                       |
| ----------- | ------------------ | -------------------------- |
| $E^{DT}$  | 直接触发关联边     | 动作$\rightarrow$ 触发器 |
| $E^{ET}$  | 环境触发关联边     | 环境$\rightarrow$ 触发器 |
| $E^{DCA}$ | 直接条件允许关联边 | 动作$\rightarrow$ 条件   |
| $E^{DCD}$ | 直接条件禁用关联边 | 动作$\rightarrow$ 条件   |
| $E^{ICA}$ | 间接条件允许关联边 | 环境$\rightarrow$ 条件   |
| $E^{ICD}$ | 间接条件禁用关联边 | 环境$\rightarrow$ 条件   |
| $E^{DA}$  | 直接动作关联边     | 动作$\rightarrow$ 动作   |
| $E^{IA}$  | 间接动作关联边     | 环境$\rightarrow$ 动作   |
| $E^{AE}$  | 环境关联边         | 动作$\rightarrow$ 环境   |

- 直接触发关联边：

  - 若规则 $r_i$ 的动作可能直接使规则 $r_j$ 的触发器成立，则存在直接触发关联边：$(v_{r_i}^A,v_{r_j}^T)\in E^{DT}$
  - 判定条件：
    - 设动作 $a\in A_{r_i}$ 会将实体 $e$ 设置为目标值 $v'$
    - 若 $T_{r_j}$ 中存在实体状态跃迁触发原子：$\Delta P_{e,v\to v'}$
    - 则加入：$(v_{r_i}^A,v_{r_j}^T)\in E^{DT}$
  - 标签：
    - $\operatorname{kind}(v_{r_i}^A,v_{r_j}^T)=\textsf{direct-trigger}$
    - $\operatorname{pol}(v_{r_i}^A,v_{r_j}^T)=+1$
  - 含义：
    - 该边表示：$r_i$ 的动作可能直接触发 $r_j$。
- 环境触发关联边：

  - 若某个环境参数可能影响规则 $r_j$ 的触发器，则存在环境触发关联边：$(v_{z,c}^E,v_{r_j}^T)\in E^{ET}$
  - 判定条件：
    - 若规则 $r_j$ 的触发器环境引用集中存在：$\rho=(z,c,\bowtie,\theta)\in E_{r_j}^T$
    - 其中：$\bowtie\in\{\uparrow,\downarrow\}$
    - 则加入：$(v_{z,c}^E,v_{r_j}^T)\in E^{ET}$
  - 方向函数：
    - $\operatorname{dir}(\rho)=\begin{cases} +1, & \bowtie=\uparrow \\ -1, & \bowtie=\downarrow \end{cases}$
  - 标签：
    - $\operatorname{kind}(v_{z,c}^E,v_{r_j}^T)=\textsf{indirect-trigger}$
    - $\operatorname{pol}(v_{z,c}^E,v_{r_j}^T)=\operatorname{dir}(\rho)$
  - 含义：
    - 若 $\operatorname{pol}=+1$，表示环境参数上升有助于触发该规则；
    - 若 $\operatorname{pol}=-1$，表示环境参数下降有助于触发该规则。
- 直接条件允许关联边：

  - 若规则 $r_i$ 的动作可能直接使规则 $r_j$ 的条件更容易满足，则存在直接条件允许关联边：$(v_{r_i}^A,v_{r_j}^C)\in E^{DCA}$
  - 判定条件：
    - 设规则 $r_j$ 的条件中存在实体状态谓词：$P_{e,\bowtie,v}\in C_{r_j}$
    - 若动作 $a\in A_{r_i}$ 将实体 $e$ 设置为 $v'$，且 $v'\bowtie v$ 成立
    - 则加入：$(v_{r_i}^A,v_{r_j}^C)\in E^{DCA}$
  - 标签：
    - $\operatorname{kind}(v_{r_i}^A,v_{r_j}^C)=\textsf{direct-condition-allow}$
    - $\operatorname{pol}(v_{r_i}^A,v_{r_j}^C)=+1$
  - 含义：
    - 该边表示：$r_i$ 的动作可能直接使 $r_j$ 的条件成立或更容易成立。
- 直接条件禁用关联边：

  - 若规则 $r_i$ 的动作可能直接使规则 $r_j$ 的条件失效，则存在直接条件禁用关联边：$(v_{r_i}^A,v_{r_j}^C)\in E^{DCD}$
  - 判定条件：
    - 设规则 $r_j$ 的条件中存在实体状态谓词：$P_{e,\bowtie,v}\in C_{r_j}$
    - 若动作 $a\in A_{r_i}$ 将实体 $e$ 设置为 $v'$，且 $\neg(v'\bowtie v)$ 成立
    - 则加入：$(v_{r_i}^A,v_{r_j}^C)\in E^{DCD}$
  - 标签：
    - $\operatorname{kind}(v_{r_i}^A,v_{r_j}^C)=\textsf{direct-condition-disable}$
    - $\operatorname{pol}(v_{r_i}^A,v_{r_j}^C)=-1$
  - 含义：
    - 该边表示：$r_i$ 的动作可能直接破坏 $r_j$ 的条件，使其不满足。
- 间接条件允许关联边：

  - 若某个环境参数上升可能使规则 $r_j$ 的条件更容易满足，则存在间接条件允许关联边：$(v_{z,c}^E,v_{r_j}^C)\in E^{ICA}$
  - 判定条件：
    - 若规则 $r_j$ 的条件环境引用集中存在：$\rho=(z,c,\bowtie,\theta)\in E_{r_j}^C$
    - 且：$\bowtie\in\{>,\ge\}$
    - 则加入：$(v_{z,c}^E,v_{r_j}^C)\in E^{ICA}$
  - 标签：
    - $\operatorname{kind}(v_{z,c}^E,v_{r_j}^C)=\textsf{indirect-condition-allow}$
    - $\operatorname{pol}(v_{z,c}^E,v_{r_j}^C)=+1$
  - 含义：
    - 该边表示：环境参数 $(z,c)$ 上升有助于满足 $r_j$ 的条件。
    - 例如，若条件为 $\text{BedroomTemperature}\ge 30$，则卧室温度上升会使该条件更容易满足。
- 间接条件禁用关联边：

  - 若某个环境参数上升可能使规则 $r_j$ 的条件更难满足或失效，则存在间接条件禁用关联边：$(v_{z,c}^E,v_{r_j}^C)\in E^{ICD}$
  - 判定条件：
    - 若规则 $r_j$ 的条件环境引用集中存在：$\rho=(z,c,\bowtie,\theta)\in E_{r_j}^C$
    - 且：$\bowtie\in\{<,\le\}$
    - 则加入：$(v_{z,c}^E,v_{r_j}^C)\in E^{ICD}$
  - 标签：
    - $\operatorname{kind}(v_{z,c}^E,v_{r_j}^C)=\textsf{indirect-condition-disable}$
    - $\operatorname{pol}(v_{z,c}^E,v_{r_j}^C)=-1$
  - 含义：
    - 该边表示：环境参数 $(z,c)$ 上升会削弱或破坏 $r_j$ 的条件。
    - 例如，若条件为 $\text{Humidity}<40$，则湿度上升会使该条件更难满足。
- 直接动作关联边：

  - 若两个规则动作作用于同一实体，则存在直接动作关联边：$(v_{r_i}^A,v_{r_j}^A)\in E^{DA}$
  - 判定条件：
    - 若存在 $a_i\in A_{r_i},\ a_j\in A_{r_j}$ 满足 $\operatorname{target}(a_i)=\operatorname{target}(a_j)$
    - 则加入：$(v_{r_i}^A,v_{r_j}^A)\in E^{DA}$（通常该关系是双向的，因此也可加入：$(v_{r_j}^A,v_{r_i}^A)\in E^{DA}$）
  - 极性：
    - 若两个动作对同一实体的目标结果一致 $\operatorname{value}(a_i)=\operatorname{value}(a_j)$，则：$\operatorname{pol}(v_{r_i}^A,v_{r_j}^A)=+1$
    - 若目标结果不一致 $\operatorname{value}(a_i)\neq\operatorname{value}(a_j)$，则：$\operatorname{pol}(v_{r_i}^A,v_{r_j}^A)=-1$
  - 标签：
    - $\operatorname{kind}(v_{r_i}^A,v_{r_j}^A)=\textsf{direct-action}$
  - 含义：
    - 该边表示：两个动作在同一实体上存在状态关联（不直接表示异常，只表示动作结果相关）。
- 间接动作关联边：

  - 若某个动作会影响某个环境参数，则该环境参数与该动作之间存在间接动作关联边：$(v_{z,c}^E,v_r^A)\in E^{IA}$
  - 判定条件：
    - 若存在环境效应项 $\eta=(z_s,\mathcal{R},c,s,\mu,\lambda,d)\in E_r^A$ 且 $z\in\mathcal{R}$
    - 则加入：$(v_{z,c}^E,v_r^A)\in E^{IA}$
  - 标签：
    - $\operatorname{kind}(v_{z,c}^E,v_r^A)=\textsf{indirect-action}$
    - $\operatorname{pol}(v_{z,c}^E,v_r^A)=s$
  - 含义：
    - 该边用于表达某个动作与环境参数之间存在环境层面的动作关联，与其它间接关联边不同，它的索引是反向的，从而形成 $v_{r_i}^A\rightarrow v_{z,c}^E\rightarrow v_{r_j}^A$。
    - 注意，该边方向为：环境 $\rightarrow$ 动作。
    - 它和环境关联边（动作 $\rightarrow$ 环境）配合使用，可以形成路径 $v_{r_i}^A\rightarrow v_{z,c}^E\rightarrow v_{r_j}^A$，从而表达两个动作通过同一环境参数产生间接动作关联。
    - 若 $s=+1$，表示动作 $A_r$ 对环境参数 $(z,c)$ 的作用方向为上升；若 $s=-1$，表示作用方向为下降。
    - 路径 $p = v_{r_i}^A\rightarrow v_{z,c}^E\rightarrow v_{r_j}^A$ 的极性为：$\operatorname{pol}(p) = \operatorname{pol}(v_{r_i}^A,v_{z,c}^E) \cdot \operatorname{pol}(v_{z,c}^E,v_{r_j}^A)$
    - 若 $\operatorname{pol}(p)=+1$，表示两个动作对同一环境参数同向相关；若 $\operatorname{pol}(p)=-1$，表示两个动作对同一环境参数反向相关。
- 环境关联边：

  - 若规则 $r_i$ 的动作会影响某个环境参数，则存在环境关联边：$(v_{r_i}^A,v_{z,c}^E)\in E^{AE}$
  - 判定条件：
    - 若存在环境效应项 $\eta=(z_s,\mathcal{R},c,s,\mu,\lambda,d)\in E_{r_i}^A$ 且 $z\in\mathcal{R}$
    - 则加入：$(v_{r_i}^A,v_{z,c}^E)\in E^{AE}$
  - 标签：
    - $\operatorname{kind}(v_{r_i}^A,v_{z,c}^E)=\textsf{env-association}$
    - $\operatorname{pol}(v_{r_i}^A,v_{z,c}^E)=s$
  - 含义：
    - 该边表示动作节点对环境参数节点产生影响。
    - 若 $s=+1$，表示动作使环境参数 $(z,c)$ 上升；若 $s=-1$，表示动作使环境参数 $(z,c)$ 下降。

##### 规则关联图

- **规则关联图**定义为一个有向带标签多图：$\mathcal{G}_{RA}=(V,E,\ell)$
  - $V=V^T\cup V^C\cup V^A\cup V^E$
  - $E=E^{intra}\cup E^{assoc}$
  - $E^{assoc}=E^{DT}\cup E^{ET}\cup E^{DCA}\cup E^{DCD}\cup E^{ICA}\cup E^{ICD}\cup E^{DA}\cup E^{IA}\cup E^{AE}$
  - **边标签函数**：$\ell:E\to \mathcal{K}\times\{+1,-1\}\times\mathcal{M}$
    - $\mathcal{K}$：边类型集合
    - $\{+1,-1\}$：边极性集合
    - $\mathcal{M}$：边元数据集合
  - **边类型集合定义**：$\mathcal{K}=\{\textsf{flow},\textsf{direct-trigger},\textsf{indirect-trigger},\textsf{direct-condition-allow},\textsf{direct-condition-disable},\textsf{indirect-condition-allow},\textsf{indirect-condition-disable},\textsf{direct-action},\textsf{indirect-action},\textsf{env-association}\}$
    - `flow` 为规则内部边类型
    - 其余九类为规则关联边类型
- **路径**：在规则关联图 $\mathcal{G}_{RA}$ 中，一条路径定义为$p=\langle v_0,v_1,\dots,v_k\rangle$
  - $(v_i,v_{i+1})\in E$
- **关联路径**：若路径 $p$ 中至少包含一条规则关联边：$\exists i,\ (v_i,v_{i+1})\in E^{assoc}$，则称 $p$ 为一条**关联路径**
- **路径极性**：路径极性定义为路径上所有边极性的乘积
  - $\operatorname{pol}(p)=\prod_{i=0}^{k-1}\operatorname{pol}(v_i,v_{i+1})$
  - 路径极性表示该路径整体对终点节点的影响方向

### 实体常态与非预期结果定义

> 非预期结果不直接由规则关联边决定，而由规则关联路径结合实体常态配置决定
> 弱非预期状态转换用于静态阶段，因为静态分析通常只能获得动作目标状态 $v'$，无法确定动作前状态 $v$；严格非预期状态转换用于运行时阶段，因为运行时可以观测真实状态变化 $v\to v'$。

- **常态实体集合**
  - 定义需要维护常态的实体集合：$\mathcal{E}_n\subseteq\mathcal{E}$
  - 需要维护常态的实体一般属于安全敏感型设备，例如门窗、消防水阀等，以检测在规则执行过程中，是否由于规则间的关联导致非预期的结果
- **常态谓词**
  - 对每个实体 $e\in\mathcal{E}_n$，定义常态谓词 $N_e:\mathcal{V}_e\to\{0,1\}$
    - $N_e(v)=1$ 表示状态 $v$ 是实体 $e$ 的常态
    - $N_e(v)=0$ 表示状态 $v$ 不是实体 $e$ 的常态
- **实体常态**定义为：$\mathcal{N}=\{N_e\mid e\in\mathcal{E}_n\}$
- **状态转换**
  - 若实体 $e$ 从状态 $v$ 变化为状态 $v'$，记为：$(e,v\to v')$
- **严格非预期状态转换**
  - 若实体从常态转移到非常态 $N_e(v)=1$，且 $N_e(v')=0$，则称：$(e,v\to v')$ 为严格非预期状态转换，动态运行时要求严格非预期，避免误报
  - 定义为：$\operatorname{Unexpected}_{\mathcal{N}}(e,v\to v')\Longleftrightarrow N_e(v)=1\land N_e(v')=0$
- **弱非预期状态转换**
  - 在静态分析阶段，如果无法确定前态 $v$，只判断目标状态 $v'$ 是否偏离常态，则定义弱非预期状态转换：$\operatorname{WUnexpected}_{\mathcal{N}}(e,v\to v')\Longleftrightarrow N_e(v')=0$，用于静态分析
  - 简写为：$\operatorname{WUnexpected}_{\mathcal{N}}(e,v')\Longleftrightarrow N_e(v')=0$
- **动作节点诱导的状态转换**：为了判断某个动作节点是否可能导致非预期状态，需要定义动作节点的状态转换集合
  - **动作后态集合**
    - 对动作 $a$，定义其目标实体为：$\operatorname{target}(a)$
    - 定义其可能目标状态集合为：$\operatorname{Post}(a)$
    - 示例：
      - `turn_on` 的 $\operatorname{Post}(a)=\{\texttt{on}\}$
      - `turn_off` 的 $\operatorname{Post}(a)=\{\texttt{off}\}$
      - `set_value(27)` 的 $\operatorname{Post}(a)=\{27\}$
  - **动作节点后态集合**：
    - 对动作节点 $v_r^A$，定义：$\operatorname{Post}(v_r^A)=\{(e,v')\mid \exists a\in A_r,\ e=\operatorname{target}(a),\ v'\in\operatorname{Post}(a)\}$
  - **动作节点非预期后态**
    - 若存在：$(e,v')\in\operatorname{Post}(v_r^A)$ 满足：$e\in\mathcal{E}_n$，且：$N_e(v')=0$，则称动作节点 $v_r^A$ 可能产生非预期后态。
    - 定义为：$\operatorname{Abn}_{\mathcal{N}}(v_r^A)\Longleftrightarrow \exists(e,v')\in\operatorname{Post}(v_r^A),\ e\in\mathcal{E}_n\land N_e(v')=0$
- **规则关联非预期结果**：规则关联本身不是非预期结果，只有当某条关联路径最终到达一个可能产生非预期后态的动作节点时，才认为该关联路径诱导了规则关联非预期结果。
  - **终止于动作节点的关联路径**
    - 设 $p=\langle v_0,v_1,\dots,v_k\rangle$ 是一条关联路径
    - 若 $v_k\in V^A$，则该路径终止于动作节点
  - **规则关联非预期结果**
    - 若路径 $p$ 满足以下条件，则称路径 $p$ 诱导一个规则关联非预期结果
      1. $p$ 是关联路径；
      2. $p$ 终止于动作节点 $v_k\in V^A$；
      3. 该动作节点可能产生非预期后态：$\operatorname{Abn}_{\mathcal{N}}(v_k)$
    - 定义：$\operatorname{UOutcome}_{\mathcal{N}}(p)\Longleftrightarrow \operatorname{AssocPath}(p)\land \operatorname{last}(p)\in V^A\land \operatorname{Abn}_{\mathcal{N}}(\operatorname{last}(p))$
      - $\operatorname{AssocPath}(p)$ 表示 $p$ 是关联路径
      - $\operatorname{last}(p)$ 表示路径终点节点
- **运行时严格非预期结果**：
  - 在运行时，可以观测到动作执行前后的实体状态 $v\to v'$，因此使用严格定义：$\operatorname{RUOutcome}_{\mathcal{N}}(p,t)\Longleftrightarrow \exists(e,v\to v')\in\operatorname{Trans}_t(\operatorname{last}(p)),\operatorname{Unexpected}_{\mathcal{N}}(e,v\to v')$
  - $\operatorname{Trans}_t(\operatorname{last}(p))$ 表示路径终点动作节点在运行时 $t$ 实际诱导的状态转换集合

### 非预期状态转换图定义

> 非预期状态转换图由规则关联图结合实体常态配置 $\mathcal{N}$ 得到，是规则关联图的子图。

- **非预期关联路径集合**
  - 定义所有诱导非预期结果的关联路径集合：$\mathcal{P}_U=\{p\mid \operatorname{UOutcome}_{\mathcal{N}}(p)\}$
- **非预期状态转换图**
  - 非预期状态转换图定义为：$\mathcal{G}_{UST}=(V_U,E_U,\ell_U)$
    - $V_U\subseteq V$，$V_U=\{v\in V\mid \exists p\in\mathcal{P}_U,\ v\in p\}$
    - $E_U\subseteq E$，$E_U=\{e\in E\mid \exists p\in\mathcal{P}_U,\ e\in p\}$
    - 边标签函数为原规则关联图标签函数的限制：$\ell_U=\ell|_{E_U}$
  - 由此：$\mathcal{G}_{UST}\subseteq \mathcal{G}_{RA}$
- **结果标注函数**
  - 由于非预期状态转换图是规则关联图的子图，不额外引入状态转换节点，因此需要一个结果标注函数记录每条非预期路径对应的非预期后态，表示关联路径 $p$ 最终可能导致哪些实体进入非常态。
  - 定义：$\operatorname{Out}_U:\mathcal{P}_U\to 2^{\mathcal{E}\times \mathcal{V}}$
    - $\operatorname{Out}_U(p)=\{(e,v')\mid (e,v')\in\operatorname{Post}(\operatorname{last}(p)),e\in\mathcal{E}_n,N_e(v')=0\}$

### 设计方案与实现

#### zone+channel感知 与 TCAE建模

本阶段的目标是将 Home Assistant 自动化规则从平台相关的 YAML 表示转化为平台无关的 $\text{TCAE}$ 规则模型。该过程以 `automations.yaml` 为输入，依次生成实体集合、实体与物理通道绑定、实体与区域绑定以及最终的 $\text{TCAE}$ 模型。前提：实体命名规范

##### 输入与输出

- **输入文件**：
  - `automations.yaml`：Home Assistant 自动化规则；
  - `config.json`：系统配置、通道集合 $\mathcal{C}$、路径配置；
  - `.env`：Home Assistant 与 AI 服务连接配置。
- **输出文件**：
  - `devices.json`：从自动化规则中提取的实体信息；
  - `channels.json`：实体 $e \in \mathcal{E}$ 与物理通道 $c \in \mathcal{C}$ 之间的绑定关系；
  - `zones.json`：实体 $e \in \mathcal{E}$ 与家庭区域 $z \in \mathcal{Z}$ 之间的绑定关系；
  - `tcae.json`：每条规则的结构化 $\text{TCAE}$ 模型。

##### Step 1：设备实体提取

系统首先解析 `automations.yaml` 中的 `trigger`、`condition` 和 `action` 字段，提取所有出现过的 Home Assistant 实体。每个实体记录其：

- `entity_id`；
- `domain`；
- 出现位置：`trigger`、`condition`、`action`；
- 可能角色：`sensor`、`actuator`、`hybrid`；
- 所属规则集合；
- 原始 YAML 片段。

实体角色根据出现位置初步判断：

- 只出现在 trigger 或 condition 中的实体，通常视为可观测实体；
- 出现在 action target 中的实体，通常视为可执行实体；
- 同时出现在观测侧和动作侧的实体，视为混合实体。

##### Step 2：实体-channel 绑定

在获得实体集合后，系统根据实体名称、实体 domain、规则描述和候选 channel 集合，为实体绑定其观测或影响的物理通道。该过程由 AI 给出初始建议，再由用户进行审核。

对于传感器类实体，绑定结果表示其观测的环境通道，例如：

```json
{
  "entity_id": "input_number.wo_shi_wen_du_chuan_gan_qi",
  "role": "sensor",
  "observes": [
    {
      "channel": "temperature",
      "value_type": "numeric",
      "confidence": 0.98
    }
  ]
}
```

对于执行器类实体，绑定结果表示其动作对环境通道的影响，例如：

```json
{
  "entity_id": "input_boolean.wo_shi_nuan_qi",
  "role": "actuator",
  "effects": [
    {
      "channel": "temperature",
      "on_direction": "+1",
      "off_direction": "-1",
      "confidence": 0.95
    }
  ]
}
```

若实体不具有明确物理通道影响，则其 `observes` 或 `effects` 可以为空，同时AI将反馈发现的新channel，用以迭代更新channels列表

##### Step 3：实体-zone 绑定

系统随后为具有观测或环境效应的实体绑定家庭区域， zone应当由用户进行绑定。zone 绑定原则如下：

- 传感器的 zone 表示其观测区域；
- 执行器的 zone 表示其源影响区域；
- 若执行器的影响可传播到多个区域，则额外记录 `reachable_zones`；
- 若未提供传播范围，则默认 `reachable_zones = [zone]`。

示例：

```json
{
  "entity_id": "input_boolean.wo_shi_nuan_qi",
  "zone": "Bedroom",
  "reachable_zones": ["Bedroom"]
}
```

##### Step 4：TCAE 模型构建

在获得规则、实体、channel 和 zone 信息后，系统将每条自动化规则转化为 TCAE 模型：

$$
r = \langle T_r, \; C_r, \; A_r, \; (E_r^T, E_r^C, E_r^A) \rangle
$$

其中：

- $T_r$ 来自 Home Assistant 的 `trigger` 字段；
- $C_r$ 来自 `condition` 字段；
- $A_r$ 来自 `action` 字段；
- $E_r^T$ 由 trigger 中的环境传感器引用生成；
- $E_r^C$ 由 condition 中的环境传感器引用生成；
- $E_r^A$ 由 action 中执行器的环境效应生成。

对于 numeric trigger，例如：

```yaml
platform: numeric_state
entity_id: input_number.wo_shi_wen_du_chuan_gan_qi
above: 30
```

若该实体绑定为：

```text
zone = Bedroom
channel = temperature
```

则生成环境触发引用：

$$
\rho = (\text{Bedroom}, \text{temperature}, \uparrow, 30) \in E_r^T
$$

对于 numeric condition，例如：

```yaml
condition: numeric_state
entity_id: input_number.ke_ting_liang_du_chuan_gan_qi
below: 50
```

若该实体绑定为：

```text
zone = LivingRoom
channel = light
```

则生成环境条件引用：

$$
\rho' = (\text{LivingRoom}, \text{light}, <, 50) \in E_r^C
$$

对于 action，例如：

```yaml
service: input_boolean.turn_on
target:
  entity_id: input_boolean.wo_shi_nuan_qi
```

若该实体绑定为：

```text
zone = Bedroom
reachable_zones = [Bedroom]
channel = temperature
on_direction = +1
```

则生成环境效应：

$$
\eta = (z_s, \mathcal{R}_e, c, s, \mu, \lambda, d)
$$

其中 $\mu$、$\lambda$、$d$ 可由默认配置、用户输入或 AI 建议给出。若当前实现不估计具体物理参数，则可采用默认值：

```text
μ = 1
λ = 0
d = +∞ 或预设持续时间
```

##### 输出

最终生成的 `tcae.json` 应包含每条规则的：

- 规则 ID；
- 规则别名；
- 结构化触发器；
- 结构化条件；
- 结构化动作；
- 触发器环境引用集 $E_T$；
- 条件环境引用集 $E_C$；
- 动作环境效应集 $E_A$。

#### 规则关联图生成算法（静态代码分析）

1. 基于TCAE 实现规则关联图生成算法，伪代码如下

```
Algorithm BuildRuleAssociationGraph(TCAE)
Input : TCAE rule set R = {r1, ..., rn}
Output: Rule Association Graph G_RA = (V, E, ell)

1  V <- empty set; E <- empty list
2  for each rule r in R do
3      add trigger node v_r^T to V
4      if C_r is not empty then
5          add condition node v_r^C to V
6      add action node v_r^A to V
7      if C_r is not empty then
8          add flow edge v_r^T -> v_r^C and v_r^C -> v_r^A
9      else
10         add flow edge v_r^T -> v_r^A
11     for each environment reference rho in E_r^T union E_r^C do
12         add environment node v_(rho.zone,rho.channel)^E to V
13     for each environment effect eta in E_r^A do
14         for each z in eta.reachable_zones do
15             add environment node v_(z,eta.channel)^E to V
16             add env-association edge v_r^A -> v_(z,eta.channel)^E
17             add indirect-action edge v_(z,eta.channel)^E -> v_r^A
18 end for
19 for each ordered rule pair (ri, rj), ri != rj do
20     for each action a in A_ri and trigger t in T_rj do
21         if a may make t true then
22             add direct-trigger edge v_ri^A -> v_rj^T
23     for each action a in A_ri and condition c in C_rj do
24         if a may make c true then
25             add direct-condition-allow edge v_ri^A -> v_rj^C
26         else if a may make c false then
27             add direct-condition-disable edge v_ri^A -> v_rj^C
28     for each action a in A_ri and action b in A_rj do
29         if target(a) = target(b) then
30             add direct-action edge v_ri^A -> v_rj^A
31 end for
32 for each rule r in R do
33     for each environment trigger reference rho in E_r^T do
34         add indirect-trigger edge v_(rho.zone,rho.channel)^E -> v_r^T
35     for each environment condition reference rho in E_r^C do
36         if increasing (rho.zone,rho.channel) helps satisfy rho then
37             add indirect-condition-allow edge v_(rho.zone,rho.channel)^E -> v_r^C
38         else
39             add indirect-condition-disable edge v_(rho.zone,rho.channel)^E -> v_r^C
40 end for

41 remove duplicate edges with identical source, target, kind, polarity and core metadata
42 assign stable edge identifiers
43 return G_RA
```

#### 非预期状态转换图生成算法（规则关联图根据实体常态配置得到的结果）

1. 基于AI判断哪些是安全敏感的设备，从而配置设备常态
2. 人工审核是否需要调整设备的实体常态配置
3. 基于实体常态配置与规则关联图，剪枝实现非预期状态转换图生成算法，伪代码如下

```
Algorithm BuildUnexpectedStateTransitionGraph(G_RA, TCAE, NormalConfig)
Input :
  G_RA = (V, E, ell), the Rule Association Graph
  TCAE rule set R = {r1, ..., rn}
  NormalConfig N, entity normal-state predicates
Output:
  G_UST = (V_U, E_U, ell_U), Unexpected State Transition Graph
  P_U, unexpected association paths
  Out_U, path-to-unexpected-post-state annotations

1  ActionPost <- empty map from action node id to post-state set
2  AbnormalActions <- empty map from action node id to unexpected post-states
3  for each rule r in TCAE.rules do
4      a_node <- node id of v_r^A
5      for each action a in A_r do
6          e <- target entity of action a
7          Posts <- abstract post-state set of a
8          for each v' in Posts do
9              add (e, v') to ActionPost[a_node]
10             if e is configured in NormalConfig and N_e(v') = 0 then
11                 add (e, v') to AbnormalActions[a_node]
12 end for
13 P_U <- empty list
14 for each terminal action node vA in AbnormalActions.keys do
15     perform bounded reverse DFS in G_RA from vA
16     during DFS, keep node sequence p and edge sequence ep
17     if p contains at least one association edge and last(p)=vA then
18         if PositiveOnly is false or path_polarity(p) = +1 then
19             add p to P_U
20             Out_U[p] <- AbnormalActions[vA]
21 end for
22 V_U <- all nodes appearing in paths P_U
23 E_U <- all edges appearing in paths P_U
24 ell_U <- restriction of ell to E_U
25 return G_UST = (V_U, E_U, ell_U), P_U, Out_U
```

#### 非预期状态处理策略生成算法（家庭环境上下文+产生关联的规则事件上下文+模板+AI判断）

- 将图中的非预期路径规约为若干“源动作规则 $r_i$ 与目标规则 $r_j$”之间的局部关联模式，然后结合家庭环境上下文、两条规则的 TCAE 上下文、关联方式、非预期后态以及处理策略模板，由 AI 选择一个可执行的处理策略。
- 核心思想如下：
  - 对于直接关联，若存在 $v_{r_i}^A \rightarrow v_{r_j}^{T/C/A}$，则构造规则对 $(r_i,r_j)$；
  - 对于间接关联，若存在 $v_{r_i}^A \rightarrow v_{z,c}^E \rightarrow v_{r_j}^{T/C/A}$，则构造规则对 $(r_i,r_j)$；
  - 只对两两规则之间的局部连接生成策略，不把完整长路径整体发送给 AI；
  - 若多个非预期路径包含同一个局部规则关联，则合并其证据、路径 ID 和非预期后态；
  - AI 的输入由四部分组成：
    1. 家庭环境上下文：实体列表、实体所属区域、实体关联物理通道、实体常态配置；
    2. 源规则 $r_i$ 的 TCAE 信息；
    3. 目标规则 $r_j$ 的 TCAE 信息；
    4. 两条规则之间的关联方式、相关边、环境中继节点、可能导致的非预期后态、处理策略模板。

| 策略编号 | 策略名称                               | 含义                                                     |
| -------: | -------------------------------------- | -------------------------------------------------------- |
|        0 | `default`                            | 默认执行，不进行干预                                     |
|        1 | `only_first_triggered`               | 只执行先触发规则，并禁止后触发规则动作                   |
|        2 | `only_later_triggered`               | 只执行后触发规则，并撤销先触发规则动作                   |
|        3 | `force_lexicographic_first`          | 强制保留规则 ID 字典序较小的规则，并撤销或禁止另一条规则 |
|        4 | `force_lexicographic_second`         | 强制保留规则 ID 字典序较大的规则，并撤销或禁止另一条规则 |
|        5 | `both_end_with_lexicographic_second` | 两条规则都执行，但以规则 ID 字典序较大的规则作为最终状态 |
|        6 | `both_end_with_lexicographic_first`  | 两条规则都执行，但以规则 ID 字典序较小的规则作为最终状态 |
|        7 | `cancel_both`                        | 取消两条规则执行                                         |

- 算法伪代码：

```
Algorithm GenerateResolutionRules(G_UST, TCAE, Devices, Channels, Zones, NormalConfig, Templates)
Input :
  G_UST        = (V_U, E_U, ell_U), unexpected state transition graph
  TCAE         = TCAE rule set R = {r1, ..., rn}
  Devices      = extracted Home Assistant entity information
  Channels     = entity-channel bindings
  Zones        = human-reviewed entity-zone bindings
  NormalConfig = entity normal-state predicates
  Templates    = predefined resolution strategy templates

Parameters:
  TerminalOnly = true
      // If true, only generate policies for local associations whose target rule
      // is the terminal unexpected-action rule.
  RPM = 10
      // AI request rate limit.

Output:
  ResolutionRules, AI-selected handling policies for terminal pairwise rule associations

1   EdgeMap <- map edge_id to edge in G_UST
2   NodeMap <- map node_id to node in G_UST
3   PairCandidates <- empty map
4   for each unexpected path p in G_UST.unexpected_paths do
5       EdgeSeq <- edges of p according to p.edges
6       NodeSeq <- nodes of p according to p.nodes
7       TerminalAction <- p.terminal_action_node
8       TerminalRule <- RuleOf(TerminalAction)        // TerminalRule is the rule whose action node produces the weak unexpected post-state.
9       for each edge e in EdgeSeq do
10          if e.kind in {direct-trigger, direct-condition-allow, direct-condition-disable, direct-action} then
11              SourceNode <- source(e)
12              TargetNode <- target(e)
13              if SourceNode is action node v_ri^A and TargetNode is component node v_rj^{T/C/A} then
14                  SourceRule <- RuleOf(SourceNode)
15                  TargetRule <- RuleOf(TargetNode)
16                  TargetComponent <- ComponentOf(TargetNode)
17                  if SourceRule = TargetRule then
18                      continue
19                  end if
20                  if TerminalOnly = true and TargetRule != TerminalRule then
21                      continue
                      // Do not generate policy for remote intermediate links.
                      // Example: in R12 -> R3 -> R15 -> R2, only R15 -> R2 is kept.
22                  end if
23                  key <- MakeCandidateKey( SourceRule, TargetRule, TargetComponent, e.kind, via_environment = null)
24                  merge edge e, path p, and p.out_U into PairCandidates[key]
25              end if
26          end if
27      end for
28      for each adjacent edge pair (e1, e2) in EdgeSeq do
29          if e1.kind = env-association and e2.kind in {env-trigger,   indirect-condition-allow,   indirect-condition-disable,   indirect-action} and target(e1) = source(e2) then
30              SourceNode <- source(e1)
31              EnvNode <- target(e1)
32              TargetNode <- target(e2)
33              if SourceNode is action node v_ri^A     and EnvNode is environment node v_{z,c}^E     and TargetNode is component node v_rj^{T/C/A} then
34                  SourceRule <- RuleOf(SourceNode)
35                  TargetRule <- RuleOf(TargetNode)
36                  TargetComponent <- ComponentOf(TargetNode)
37                  EnvInfo <- (zone(EnvNode), channel(EnvNode))

38                  if SourceRule = TargetRule then
39                      continue
40                  end if

41                  if TerminalOnly = true and TargetRule != TerminalRule then
42                      continue
                      // Only keep indirect local associations entering the
                      // terminal unexpected rule.
43                  end if
44                  key <- MakeCandidateKey(SourceRule, TargetRule, TargetComponent, e2.kind, via_environment = EnvInfo )
45                  merge edge pair (e1, e2), path p, and p.out_U into PairCandidates[key]
46              end if
47          end if
48      end for
49  end for
50  HomeContext <- BuildHomeContext(Devices, Channels, Zones, NormalConfig)
51  ResolutionRules <- empty list
52  for each candidate q in PairCandidates do
53      ri <- q.source_rule_uid
54      rj <- q.target_rule_uid
55      SourceRuleContext <- TCAE[ri]
56      TargetRuleContext <- TCAE[rj]
57      AIPayload <- {
58          home_context: HomeContext,
59          source_rule: SourceRuleContext,
60          target_rule: TargetRuleContext,
61          association_candidate: q,
62          unexpected_outcomes: q.unexpected_outcomes,
63          strategy_templates: Templates,
64          constraints: {
65              select_exactly_one_strategy: true,
66              strategy_id_must_be_one_of: TemplateIDs(Templates),
67              return_strict_json_only: true
68          }
69      }
70      wait according to RPM limit
71      AIResponse <- QueryAI(AIPayload)
72      Policy <- ValidateAndNormalize(AIResponse, Templates)
73      if Policy.confidence < ReviewThreshold then
74          Policy.needs_human_review <- true
75      end if
76      add {
77          resolution_rule_id,
78          candidate_id: q.candidate_id,
79          source_rule_uid: ri,
80          target_rule_uid: rj,
81          target_component: q.target_component,
82          association_kind: q.association_kind,
83          association_mode: q.association_mode,
84          via_environment: q.via_environment,
85          unexpected_outcomes: q.unexpected_outcomes,
86          path_ids: q.path_ids,
87          edge_ids: q.edge_ids,
88          policy: Policy
89      } to ResolutionRules
90  end for
91  return ResolutionRules
```

- 保守回退策略：
  - 当 AI 不可用或输出无效时，系统使用保守回退策略：
    - 若非预期后态安全等级为 `high` 或 `critical`，且源规则与终止规则不同，则默认选择 `S2_ONLY_FIRST_BLOCK_LATER`，并标记 `needs_human_review=true`；
    - 若非预期后态为 `critical` 但关联关系不清晰，则默认选择 `S8_CANCEL_BOTH`，并要求人工审核；
    - 其他情况选择 `S1_DEFAULT`，仅报告并等待运行时严格验证。

#### 图游走算法

1. 基于非预期状态转换图，实现图游走算法，伪代码如下

```

```

#### Agent交互上下文信息

由于智能家居系统中的设备命名与场景描述高度依赖用户个人习惯，系统放弃采用任何针对特定语言或拼音的硬编码规则，转而利用大语言模型（LLM）充当通用语义推导桥梁。为确保生成的绑定关系、常态配置和处理策略在逻辑上具备极高的置信度与严格的自洽性，本系统在各阶段与 Agent 交互时均采用经过精心编排的**结构化上下文（Structured Context）**，并明确定义其系统提示词设计原则及反馈结果规范。

##### 环境绑定

在物理通道（Channel）绑定阶段，系统需要为从 `automations.yaml` 中提取的每一台实体设备绑定其在物理空间中观测或产生效应的环境维度。

1. **输入上下文组织编排 (Input Context Alignment)**：
   - **候选通道集合（Candidate Channels, $\mathcal{C}$）**：来自于 `config.json`，用以约束 AI 绑定的边界。
   - **实体语义画像（Entity Semantic Profile）**：包含实体 ID（`entity_id`）、设备注册元数据 `registry`（包含原厂名称、设备描述等稳定标识，不依赖特定拼音字串），以及当前实体的多条**规则引用上下文（raw_contexts）**。
   - **规则引用上下文（Rule Contexts）**：提供实体在不同自动化规则中出现的实际位置（`section`，如 `trigger`/`condition`/`action`）、相关的触发平台/条件机制、调用的服务（`service`）、具体的动作行为、以及动作后的目标状态值（`post_value`），辅以其原始 YAML 片段（`node_excerpt`）作为底层语义支撑。
2. **系统提示词设计原则 (System Prompt Guidelines)**：
   - 存放在 `config.json` 的 `system_prompts.channels_binding` 中。
   - 核心任务：根据实体在自动化规则中的上下文，推断出其物理通道绑定（`observes`/`effects`）与实体角色（`role`）。
   - 约束：
     - **中性语义假设**：不预设特定的命名语言，将所有非英语的拼音、简称视为中性语义特征，仅提取其中的空间与物理联系。
     - **角色敏感区分**：精确区分传感器（Sensor）与执行器（Actuator）。传感器仅有观测关系 `observes`，执行器则根据不同的服务操作（Operations，如 `turn_on`/`turn_off`）绑定方向极性明确的物理通道效应（Effects，极性为 $+1, -1, 0, \text{unknown}$），即 `effects_by_operation`。
     - **跨通道效应支持**：支持单一实体影响多个物理通道（如空调在 `turn_on` 时对温度通道方向为 $-1$, 对湿度通道方向也为 $-1$）。
     - **新通道发现（Channel Discovery）**：当现有候选通道无法完全表达实体的物理效应时，拒绝生硬绑定，应在 `proposed_channels` 中提出新增通道建议。
3. **反馈结果规范 (Response Schema Specifications)**：
   - 必须反馈符合以下结构的严格 JSON 对象：
     ```json
     {
       "bindings": [
         {
           "entity_id": "input_number.ke_ting_liang_du_chuan_gan_qi",
           "role": "sensor",
           "observes": [
             {
               "channel": "light",
               "value_type": "numeric",
               "confidence": 0.98,
               "reason": "Used in trigger of rule LivingRoom_Light_Auto observing light changes."
             }
           ],
           "effects": [],
           "effects_by_operation": {},
           "needs_human_review": false,
           "notes": ""
         }
       ],
       "proposed_channels": []
     }
     ```

##### 实体常态配置

在静态图分析中，弱非预期状态转换的剪枝高度依赖“实体常态配置”。该交互旨在为安全敏感型实体在没有明确合理自动化语义上下文时，锚定一个应当保持的安全或偏好状态。

1. **输入上下文组织编排 (Input Context Alignment)**：
   - **候选实体集合（Action Targets）**：主要选择在 TCAE 规则中被动作所控制的所有候选实体。
   - **空间区域绑定（Zone Bindings）**：合并前述由用户在 CLI 交互中确认的设备源区域（`source_zones`）与可达区域（`reachable_zones`），确保 AI 能够理解设备的物理覆盖范围（例如开放式厨房可跨区域波及客厅）。
   - **物理通道绑定（Channel Bindings）**：传递该实体已绑定的物理角色、通道以及效应方向。
   - **规则动作后态集（TCAE Post-States）**：搜集并列举出该实体在所有 TCAE 规则中执行动作后会被赋予的全部抽象目标后态（`possible_post_values_from_tcae`），确保 AI 生成的常态值能与 TCAE 静态推导状态完全对齐。
2. **系统提示词设计原则 (System Prompt Guidelines)**：
   - 存放在 `config.json` 的 `system_prompts.normal_config` 中。
   - 核心任务：根据设备特性、空间位置与关联规则，判断该实体是否为安全/安防/资源/隐私敏感型设备，推导其安全的常态谓词（Normal-state predicate）。
   - 约束：
     - **紧凑配置原则**：避免给普通的舒适度设备（如氛围灯、加湿器）强行配置常态。重点聚焦于出入控制（门锁、窗户）、安全防御（消防水阀、烟雾报警）、隐私防范与临界资源保护（阀门、大功率用电器）。
     - **状态严格对齐**：其常态值 `normal_values` 必须使用可直接与动作后态（post-state）相比较的原子值（如 `"on"`, `"off"`, 数值）。
     - **动态调整许可**：允许一个实体在不同物理和空间特征下有多个可接受的常态值，用列表呈现。
3. **反馈结果规范 (Response Schema Specifications)**：
   - 必须反馈符合以下结构的严格 JSON 对象：
     ```json
     {
       "normal_entities": [
         {
           "entity_id": "input_boolean.wo_shi_nuan_qi",
           "normal_values": ["off"],
           "abnormal_values": ["on"],
           "category": "safety",
           "safety_level": "medium",
           "confidence": 0.95,
           "reason": "Heaters should remain off by default to save energy and prevent overheating when unsupervised, unless turned on by rules in a cold context.",
           "needs_human_review": true
         }
       ]
     }
     ```

##### 非预期状态处理策略生成
