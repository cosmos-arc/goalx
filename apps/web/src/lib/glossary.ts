/**
 * 票 18 内容源（票 08 Answer 定稿 = TS 常量模块，纯前端零端点）：首批 10 词条。
 *
 * 口径准绳与同步方式（谁改哪边）：`.scratch/goalx-quant/glossary.md`（票 37 验收后
 * 转正为 `docs/plans/goalx-quant/glossary.md`，届时本注释指向它）是唯一口径准绳——
 * 口径变更先改那边，再同步转写进本模块；本模块只是 UI 呈现层，不得自创口径，
 * 也不得简化掉准绳里的警示（如"可投≠必成交""EV 是诊断量非机会信号"）。
 * 词条三要素（定义 / 判读方向 / 数字实例）齐全才上线（票 18 不变量：判读方向必填）。
 */

/** 词条结构（票 08 定稿字段：id/term/aliases/definition/direction/example/caution）。 */
export type GlossaryEntry = {
	/** 稳定 id：页内 tooltip 接入（GlossaryTerm）与测试锚点用。 */
	id: string;
	/** 词条名（词典页卡片标题；页内指标名默认展示文本）。 */
	term: string;
	/** 别名/相关叫法：仅参与检索命中，不改变词条名。 */
	aliases: string[];
	/** 定义（口径转写自 glossary.md）。 */
	definition: string;
	/** 判读方向（必填：高了好还是低了好 / 怎么读；缺失不上线）。 */
	direction: string;
	/** 中文数字实例。 */
	example: string;
	/** 口径警示（glossary.md 的警示逐条保留，不得删减）。 */
	caution: string;
};

/** 首批 10 词条（票 08 定稿清单：今日五件套 + 全局三件套 + 验证两件套，按该顺序）。 */
export const GLOSSARY: GlossaryEntry[] = [
	{
		id: "ev",
		term: "EV",
		aliases: ["期望值", "EV 偏差", "ev_deviation", "机会级 EV"],
		definition: "EV(H/D/A) = 欧共识概率 × 竞彩价 − 1：按市场共识概率评估某一向的期望收益率。",
		direction: "正 = 正期望（红）、负 = 负期望（绿），近零中性；|EV|≥5% 不当机会看，标琥珀偏差徽章。",
		example: "欧共识 45% × 竞彩价 2.30 − 1 = +3.5%，即每投 100 元按市场共识期望赚 3.5 元。",
		caution:
			"EV 是诊断量，不是机会信号：|EV|≥5% 标 ev_deviation，默认判为数据/口径偏差而非机会。今日页 EV 与回测的“模型 EV”不同源——今日页是市场共识视角的诊断量。",
	},
	{
		id: "eu-consensus",
		term: "欧共识 p",
		aliases: ["欧共识", "共识概率", "市场共识", "Shin 去水", "去晦"],
		definition: "多 book 三向价取均价后经 Shin 去水（去晦）得到的概率：市场对真实概率的估计，是模型与 EV 的基准。",
		direction: "基准量，无独立好坏方向：模型概率与 EV 都相对它计算。",
		example: "五家 book 主胜均价 2.50，表面隐含 40%，Shin 去水后主胜共识 p ≈ 38%（水位被剔除）。",
		caution: "欧共识由欧赔推得、与竞彩价无关；参与报价的 book 数不足时共识可信度下降（见 books 词条）。",
	},
	{
		id: "eligibility",
		term: "资格徽章",
		aliases: ["资格", "资格判定", "had_quote.status", "可投", "拒绝", "证据未知"],
		definition:
			"竞彩 H/D/A 报价的证据链判定（had_quote.status）：可投 = 证据链完整且新鲜（报价年龄与两源时差均 ≤300s）；拒绝 = 已停售/已开赛/报价过期/竞彩三向不全等；证据未知 = 证据不足。",
		direction: "蓝 = 可投（可进选注流程）、红 = 拒绝（附原因）、灰框 = 证据未知。",
		example: "报价年龄 120s、两源时差 60s，均 ≤300s → 可投；已停售 → 拒绝并标注原因。",
		caution: "可投 ≠ 必成交：可投只说明证据完整且新鲜，不保证锁定价能成交。",
	},
	{
		id: "single",
		term: "单固",
		aliases: ["单关", "单固资格", "single_eligible"],
		definition: "竞彩官方单关固定奖金资格：有单固资格的场次可单关投注，否则只能作为串关腿。",
		direction: "有 = 可作单关；无 = 只能串关（如 2串1 的第二腿）。",
		example: "周六001 标单固 → 可直接 100 元单关主胜；周六002 非单固 → 须与另一场组成 2串1。",
		caution: "单关资格以提交时服务器校验为准，前端标记不替代判定。",
	},
	{
		id: "books",
		term: "books",
		aliases: ["book 数", "三向报价", "few_books", "样本少"],
		definition: "给出完整 H/D/A 三向报价的欧赔 book 数，反映市场共识的样本厚度。",
		direction:
			"越多越稳；<4 标 low_confidence（琥珀“共识低置信”警示），<3 同时命中 few_books（展示层合并为一个显示位，见共识低置信词条）。",
		example: "8 家 book 给全三向 → books = 8；只有 2 家 → books = 2，标 low_confidence（同时命中 few_books 区间）。",
		caution:
			"books <4：样本薄，欧共识 p 与 EV 的可信度随之下降（few_books 语义被低置信覆盖，两标并打时只显示低置信文案）。",
	},
	{
		id: "low-confidence",
		term: "共识低置信",
		aliases: ["低置信", "low_confidence", "共识分母护栏", "样本薄"],
		definition:
			"共识分母护栏：完整三向 book 数 <4 时，共识概率可信度不足，打低置信标记（琥珀警示）；阈值 4 为工程初值（常量在后端 odds_math.py），不改共识概率本身（Shin/均价口径不变）。",
		direction:
			"标记出现 = 该场共识 p 与共识 EV 只作弱参考——分母越薄，去水共识越接近单 book 报价；亚洲联赛（日职/韩K/中超/澳超）覆盖缩水时常见。",
		example: "某亚洲联赛场次只有 3 家 book 报全三向 → books = 3 < 4，标共识低置信；books = 4 及以上不打标。",
		caution:
			"低置信是展示层提示，不改变共识数值；books <3 的场次同时命中 few_books，展示层合并为一个显示位只显示低置信文案（4>3 覆盖其区间）。此合并方案与阈值 4 均待人追认（票 39）。",
	},
	{
		id: "paper-vs-live",
		term: "纸面 vs 真金",
		aliases: ["paper", "live", "纸面", "真金", "模拟盘"],
		definition: "paper（纸面，0 真钱）与 live（真金）两套账本分开统计；Bankroll 资金池只受 live 影响。",
		direction: "两套账分别看、不混算：真金动资金池，纸面只做验证记录。",
		example: "纸面单关 +550 元不改变真金余额；真金下注 ¥10 立即从资金池扣减 ¥10。",
		caution: "纸面与真金分别统计、不混算——总览、历史、验证各页都按模式分列。",
	},
	{
		id: "pnl-roi",
		term: "盈亏与 ROI",
		aliases: ["ROI", "盈亏", "收益率", "等权", "roi_stake_weighted"],
		definition:
			"盈亏 = 已结算注的兑付 − 注金；ROI 两口径分列（票 34 起不混称）：roi（等权）= 每注收益率平均；roi_stake_weighted = 总盈利/总投入。",
		direction: "≥0 = 长期为正才可持续；展示层红 = 正、绿 = 负（正负号为准）；未结注不计入。",
		example:
			"注 A 投 100 赚 50（+50%），注 B 投 300 亏 30（−10%）：等权 ROI = (+50% − 10%)/2 = +20%；注金加权 = (+50 − 30)/400 = +5%。",
		caution: "等权与注金加权两口径分列、不混称；当前回测为负期望（回测 skill≈−3.75%，见 skill 词条），不可作实盘依据。",
	},
	{
		id: "forward-inclusion",
		term: "前瞻纳入",
		aliases: ["前瞻", "前瞻排除", "前瞻覆盖", "forward", "post_kickoff_only", "no_forecast", "no_market_baseline"],
		definition:
			"前瞻验证只纳入开球前（issued_at < 开球）发出的最新 Forecast，对照同期欧共识基准；覆盖四态：scored / no_forecast / post_kickoff_only / no_market_baseline。",
		direction: "纳入 = 开球前发出（无泄漏）；开球后才发出的预测一律排除。",
		example: "预测 19:00 发出、20:00 开球 → 纳入计分（scored）；20:30 才发 → 排除（post_kickoff_only）。",
		caution: "开球后补发的预测不纳入（防时间泄漏）；组内 <30 场标样本不足；验证三条件只认前瞻，回测不作为门槛。",
	},
	{
		id: "clv",
		term: "CLV",
		aliases: ["clv_prob", "beat", "beat_rate", "收盘", "买在好价", "closing line"],
		definition:
			"clv_prob = 收盘基准概率 − 1/锁定赔率：收盘基准相对锁定价的剩余价值，>0 即 beat；beat_rate = 票级 CLV>0 的占比。收盘基准自票 40 起三级分层取锚（见 CLV 基准分层词条）。",
		direction: "正 = 买在好价（收盘优于锁定）、负 = 买贵了；beat_rate 门槛 ≥60%。",
		example: "锁定赔率 2.00（隐含 50%），收盘概率 51% → CLV = +1%，beat；持续 +0.5%~3% 已是强表现（Pinnacle 经验）。",
		caution:
			"票级联合口径用于串关，两腿独立已声明；200 注门槛的唯一分母是 unique_bets（按模式+选项+锁定价+时点去重），腿数不凑。",
	},
	{
		id: "clv-basis",
		term: "CLV 基准分层",
		aliases: ["基准分层", "close_basis", "主锚", "辅锚", "Pinnacle 主锚", "Betfair 辅锚", "legacy", "mixed"],
		definition:
			"收盘基准三级取锚（票 40）：Pinnacle 主锚（sharp 书，closing 行业无偏，单独 Shin）→ Betfair 交易所辅锚（back 价按佣金调整有效赔率后归一化，佣金率默认 2%、区间 2–5% 参数化）→ 多 book 共识 Shin fallback（分层前唯一口径）；基准来源随 CLV 记录标注（close_basis）。",
		direction:
			"锚级越靠前基准越可信：主锚 > 辅锚 > 共识；高 margin 书（1xBet 类）只进共识、永不作基准。不同基准的 CLV 分布不混为一谈。",
		example:
			"某场收盘有 Pinnacle 2.10/3.40/3.60 → close_basis=pinnacle（单书 Shin）；无 Pinnacle 有 Betfair → betfair_ex（back 价扣佣金）；两者皆无 → consensus（多书均价 Shin）。",
		caution:
			"分层只对新对账行生效：历史行标 legacy（分层前共识口径）不重算，新旧口径在报表分列、并行呈现一个窗口期（建议至下一整轮销售周结束，待人追认）；串关两腿基准不同计 mixed，腿级见逐腿标注。",
	},
	{
		id: "skill",
		term: "skill 与前瞻 skill",
		aliases: ["skill", "前瞻 skill", "RPS", "walk-forward", "模型能力"],
		definition:
			"skill = 1 − L_model/L_market（主指标 RPS 损失比）：模型相对市场的损失差。回测 skill（walk-forward）与前瞻 skill（开球前 Forecast × 同期欧共识基准）分开看。",
		direction: "≥0 是系统唯一通过线；负 = 模型不如市场。",
		example: "模型 RPS 0.98、市场 RPS 1.00 → skill = 1 − 0.98/1.00 = +0.02。",
		caution: "三条件只认前瞻 skill（无泄漏、回测可过拟合）；当前回测 skill≈−3.75%，不可作实盘依据。",
	},
	{
		id: "model-prob",
		term: "模型概率与模型 EV",
		aliases: ["模型概率", "模型 EV", "DC 模型", "Forecast", "model EV"],
		definition:
			"模型概率 = DC（Dixon-Coles）模型对该场主/平/客的预测概率（赛前最新一条 Forecast）；模型 EV = 模型概率 × 竞彩价 − 1：按自家模型评估某一向的期望收益率。",
		direction: "模型概率与去水共识对照，偏离即研究线索；模型 EV 红 = 正、绿 = 负（红涨绿跌），近零中性。",
		example: "模型主胜 55% × 竞彩价 2.00 − 1 = +10%；共识主胜只有 50% → 模型比市场更看好主胜 5 个百分点。",
		caution:
			"模型 EV 与共识 EV（今日/场次页）不同源：前者信自家模型，后者信市场共识；模型当前前瞻 skill 尚未过线（见 skill 词条），模型 EV 只作研究对照，不作机会信号。仅五大联赛在售场次有模型覆盖。",
	},
	{
		id: "book-deviation",
		term: "书价偏差",
		aliases: ["偏差", "逐书偏差", "公司分歧", "book deviation", "高亮"],
		definition:
			"单 book 的归一化隐含概率与去水共识概率之差；研究页对 |偏差| ≥5 个百分点的报价琥珀标注并给方向（↑ 偏高 / ↓ 偏低）。",
		direction: "偏高 = 该 book 比共识更看好该向；偏低 = 更不看好；多家同向偏离 = 公司间真实分歧。",
		example: "共识主胜 50%，某 book 主胜价 1.80（隐含 55.6%）→ 偏高 +5.6 个百分点，标琥珀 ↑。",
		caution:
			"个别 book 定价含限额/风控策略与延迟，偏差≠错价；books<3 时共识本身不可靠（见 books 词条），偏差判读随之失效。",
	},
	{
		id: "score-matrix",
		term: "比分矩阵推导",
		aliases: ["矩阵推导", "比分矩阵", "10×10", "ScoreMatrix", "进球类概率", "ttg 概率", "crs 概率"],
		definition:
			"进球类玩法（总进球 ttg/比分 crs）的概率不是独立建模，而是从 canonical 10×10 比分概率矩阵（Dixon-Coles）推导的边际视图：ttg = 矩阵反对角求和（8 档，7+ 归并尾部），crs = 28 精确格 + 胜/平/负其他三档。",
		direction:
			"同一场比赛的 ttg 与 crs 出自同一矩阵，隐含概率必然一致——这是矩阵被定为 canonical 的理由；单市场独立建模会破坏一致性，不允许。",
		example: "模型 λ 主 1.4/客 1.3 → 矩阵 → P(总进球 2)≈24.5% = Σ矩阵反对角(h+a=2)；P(比分 1:1)=矩阵(1,1) 格。",
		caution:
			"矩阵来自 DC 模型 Forecast（仅五大联赛在售场次覆盖，无 Forecast 概率空缺不伪造）；由矩阵推得的 EV 是模型×竞彩价口径（见 model-prob 词条），模型前瞻 skill 未过线前是研究对照的诊断量，不是机会信号。",
	},
	{
		id: "kelly",
		term: "Kelly 与 ¼ fractional Kelly",
		aliases: ["Kelly", "凯利", "fractional Kelly", "f*", "满 Kelly"],
		definition:
			"Kelly（对数效用）把 edge 转化为注额比例的最优解：f* = EV/(odds−1)（按 decimal 赔率与单位 EV）。实务取分数（goalx 定 1/4）以对称化模型概率的估计误差——超注惩罚是二次的、少注损失是线性的。",
		direction:
			"满 Kelly 是理论上限而非建议值：概率估计有误差时满注会放大回撤，故按 ¼ 起步；随前瞻证据累积再评估放宽到 1/2。EV≤0 时 Kelly 为负，一律建议 ¥0。",
		example:
			"EV +10%、赔率 2.00 → f* = 0.10/1.00 = 10%，取 1/4 = 2.5%：bankroll ¥10,000 建议单注 ¥250（2.5% 在 1–5% 硬区间内，不截断）。",
		caution:
			"Kelly 的最优性以真实概率已知为前提，goalx 的概率是模型估计——这是取 ¼ 分数的理由；倍投/斐波那契等 progression 系不改 EV 只重排破产路径，且污染 CLV/skill 统计，已全部否决（research/staking-plans.md）。",
	},
	{
		id: "stake-advice",
		term: "建议仓位",
		aliases: ["建议注额", "stake-advice", "仓位建议", "flat", "单注 1–5%"],
		definition:
			"系统给出的只读注额建议（不自动改单）：纸面期一律 flat（红线）——bankroll×2% 截断到 1–5% 区间、下限竞彩最低 ¥2；真金期 = ¼ fractional Kelly 再过单注 1–5% 硬上限；EV≤0 一律 ¥0。",
		direction:
			"采纳与否由操作者确认：建议注额只回答“这一注投多少”，档位与截断理由随建议展示（flat/¼Kelly/已按上限截断/EV≤0）。",
		example:
			"bankroll ¥5,004：纸面 flat → 2% = ¥100.08；真金 EV+30% @1.50 → ¼Kelly=15% 截断到 5% = ¥250.20；EV −2% → ¥0（无价值不投）。",
		caution:
			"只读建议，不自动写入注额——用户可偏离建议落注（偏离记录供事后分析）；串关注额=单关口径（传联合赔率/联合 EV，整注一个 Kelly 不分腿）；纸面期的 flat 红线保证 skill/CLV 验证指标无偏，任何比例策略在前瞻 skill 过线前不启用。",
	},
];

/** 词条 id 联合类型：页内 tooltip 接入处（GlossaryTerm）的合法取值域。 */
export type GlossaryId = (typeof GLOSSARY)[number]["id"];

const GLOSSARY_BY_ID = new Map(GLOSSARY.map((entry) => [entry.id, entry]));

/** 按 id 取词条（未知 id 返回 undefined，GlossaryTerm 据此快速失败）。 */
export function glossaryEntry(id: string): GlossaryEntry | undefined {
	return GLOSSARY_BY_ID.get(id);
}

/** 词典页检索（票 08 定稿：按 term / aliases / definition 过滤，大小写不敏感，空查询返回全部）。 */
export function filterGlossary(query: string): GlossaryEntry[] {
	const normalized = query.trim().toLowerCase();
	if (normalized === "") {
		return GLOSSARY;
	}
	return GLOSSARY.filter(
		(entry) =>
			entry.term.toLowerCase().includes(normalized) ||
			entry.aliases.some((alias) => alias.toLowerCase().includes(normalized)) ||
			entry.definition.toLowerCase().includes(normalized),
	);
}
