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
  - [ ] zone+channel感知
  - [ ] TCAE建模
  - [ ] 规则关联图生成算法（静态代码分析）
  - [ ] 非预期状态转换图生成算法（规则关联图根据实体常态配置得到的结果）
  - [ ] 非预期状态处理策略生成算法（家庭环境上下文+产生关联的规则事件上下文+模板+AI判断）
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
    - $\operatorname{kind}(v_{z,c}^E,v_{r_j}^T)=\textsf{env-trigger}$
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
  - **边类型集合定义**：$\mathcal{K}=\{\textsf{flow},\textsf{direct-trigger},\textsf{env-trigger},\textsf{direct-condition-allow},\textsf{direct-condition-disable},\textsf{indirect-condition-allow},\textsf{indirect-condition-disable},\textsf{direct-action},\textsf{indirect-action},\textsf{env-association}\}$
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

本阶段的目标是将 Home Assistant 自动化规则从平台相关的 YAML 表示转化为平台无关的 $\text{TCAE}$ 规则模型。该过程以 `automations.yaml` 为输入，依次生成实体集合、实体与物理通道绑定、实体与区域绑定以及最终的 $\text{TCAE}$ 模型。

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

若实体不具有明确物理通道影响，则其 `observes` 或 `effects` 可以为空。

##### Step 3：实体-zone 绑定

系统随后为具有观测或环境效应的实体绑定家庭区域。zone 绑定原则如下：

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

$$r = \langle T_r, \; C_r, \; A_r, \; (E_r^T, E_r^C, E_r^A) \rangle$$

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

$$\rho = (\text{Bedroom}, \text{temperature}, \uparrow, 30) \in E_r^T$$

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

$$\rho' = (\text{LivingRoom}, \text{light}, <, 50) \in E_r^C$$

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

$$\eta = (z_s, \mathcal{R}_e, c, s, \mu, \lambda, d)$$

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


```

#### 非预期状态转换图生成算法（规则关联图根据实体常态配置得到的结果）

1. 基于AI判断哪些是安全敏感的设备，从而配置设备常态
2. 人工审核是否需要调整设备的实体常态配置
3. 基于实体常态配置与规则关联图，剪枝实现非预期状态转换图生成算法，伪代码如下

```


```

#### 非预期状态处理策略生成算法（家庭环境上下文+产生关联的规则事件上下文+模板+AI判断）

1. 基于TCAE与规则非预期状态转换图和非预期状态处理策略模板模板提供的上下文信息，利用AI实现启发式非预期状态处理策略自动配置
  - 模板：
    1. 默认执行即可，不做处理
    2. 只执行先触发的规则，并禁止后触发规则的动作
    3. 只执行后触发的规则，并撤销先触发规则的动作
    4. 强制执行关联对中的第一条规则（按规则id字典序排），并撤销/禁止第二条规则的动作
    5. 强制执行关联对中的第二条规则（按规则id字典序排），并撤销/禁止第一条规则的动作
    6. 两条规则都执行，但是以第二条规则（按规则id字典序排）为结尾（执行先后要求）
    7. 两条规则都执行，但是以第一条规则（按规则id字典序排）为结尾（执行先后要求）
    8. 取消两条规则的执行
2. 人工审核是否需要调整非预期状态处理策略

#### 图游走算法

1. 基于非预期状态转换图，实现图游走算法，伪代码如下

```

```