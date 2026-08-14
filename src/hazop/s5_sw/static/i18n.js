/*
 * i18n.js — 中文/English toggle for the HAZOP-AI dashboard.
 *
 * Strategy: the DOM stays English (source of truth); a TreeWalker +
 * MutationObserver rewrite visible text nodes from a dictionary when the
 * language is Chinese.  EXACT holds whole-string translations (keys are
 * whitespace-normalized); PATTERNS handles strings with interpolated
 * numbers/tags.  Originals are kept in WeakMaps so switching back to
 * English is lossless.
 *
 * Deliberately NOT translated: equipment tags, Cypher, code/pre blocks,
 * <option>s without a value attribute (their text IS the submitted value),
 * and prose that quotes the English KB corpus (findings, snippets,
 * critic output) — translating cited evidence would falsify the audit
 * trail, so it stays verbatim.
 */
(function () {
  'use strict';

  /* ---------------- glossary (single terms, used by patterns) -------- */
  const GLOSS = {
    'compressor': '压缩机', 'vessel': '容器', 'valve': '阀门',
    'relief valve': '安全阀', 'relief_valve': '安全阀',
    'check valve': '止回阀', 'check_valve': '止回阀',
    'instrument': '仪表', 'line': '管线', 'tank': '储罐', 'pump': '泵',
    'heat exchanger': '换热器', 'heat_exchanger': '换热器',
    'equipment': '设备', 'connector': '连接符', 'arrow': '箭头',
    'junction': '交汇点', 'text': '文本', 'filter': '过滤器',
    'cooler': '冷却器', 'separator': '分离器', 'dryer': '干燥器',
    'silencer': '消声器',
  };
  const PARAM_ZH = {Flow: '流量', Pressure: '压力',
                    Temperature: '温度', Level: '液位'};
  function devZh(gw, p) {
    const z = PARAM_ZH[p];
    if (!z) return null;
    switch (gw) {
      case 'No/Not':     return '无' + z;
      case 'More':       return p === 'Flow' ? '流量过大' : z + '过高';
      case 'Less':       return p === 'Flow' ? '流量过小' : z + '过低';
      case 'Reverse':    return z + '反向';
      case 'As well as': return z + '伴随异常';
      case 'Part of':    return z + '组成异常';
      case 'Other than': return z + '异常';
      case 'Early':      return z + '过早';
      case 'Late':       return z + '过迟';
    }
    return null;
  }

  /* ---------------- exact whole-string translations ------------------ */
  const EXACT = {
    /* chrome */
    'HAZOP-AI — Integrated Dashboard': 'HAZOP-AI — 综合仪表盘',
    'integrated view — unit 2401 · L1 extraction → L2 knowledge → L3 reasoner':
      '综合视图 — 2401 装置 · L1 提取 → L2 知识层 → L3 推理器',
    'Overview': '总览',
    'L1 · Extraction': 'L1 · 图纸提取',
    'L2 · Graph Explorer': 'L2 · 图谱浏览',
    'LLM Lab': 'LLM 实验室',
    'L2 · Knowledge Base': 'L2 · 知识库',
    'L3 · HAZOP Worksheet': 'L3 · HAZOP 工作表',
    '§4 · Model Gates': '§4 · 模型验收门',
    'Tasks · RTM': '任务 · 需求追踪',
    'loading…': '加载中…',

    /* overview */
    'L1 · Knowledge layer': 'L1 · 知识层',
    'L2 · Knowledge layer': 'L2 · 知识层',
    'L3 · AI reasoner': 'L3 · AI 推理器',
    'geometric pipe segments, 9 sheets': '条几何管段（共 9 张图纸）',
    'instrument bubbles': '仪表符号',
    'equipment items': '设备台数',
    'directed segments': '已定向管段',
    'nodes': '节点',
    'equipment-level plant graph': '设备级装置图谱',
    'connections': '连接',
    'verified flow direction': '已验证流向',
    'direction conflicts': '流向冲突',
    'anti-parallel pairs (intercooler loops)': '反平行对（级间冷却回路）',
    'server not running': '服务未运行',
    'browser ↗': '浏览器 ↗',
    '(live)': '（在线）',
    'guideword × parameter matrix': '引导词 × 参数矩阵',
    'generation': '生成模型',
    'grounding gate': '溯源校验门',
    'tag-validation, rejects kept for audit': '位号校验，被拒项保留供审计',
    'direction-aware': '流向感知',
    'unverified edges flagged, never behind 0.99 claims':
      '未验证连接均予标记，绝不伪装高置信结论',
    'risk ranking': '风险分级',
    'blank — human-only (FR-ARE-6)': '留空 — 仅限人工（FR-ARE-6）',
    'KB ingestion report': '知识库入库报告',
    'How this fits together': '整体架构说明',

    /* L1 tab */
    'Process sheets — extraction overlays': '工艺图纸 — 提取结果叠加图',
    'Deterministic geometry extraction from the vector P&ID: instruments, valves (with PSV/check subclasses), equipment, pipe runs, flow arrows. Full accept/reject validation lives in the':
      '基于矢量 P&ID 的确定性几何提取：仪表、阀门（含安全阀/止回阀子类）、设备、管线、流向箭头。完整的接受/拒绝人工校验请使用',
    'Stage 1 validator app': 'Stage 1 校验器',
    'prefix': '图号前缀',

    /* graph explorer */
    'Run ▶': '运行 ▶',
    'Plant database': '装置数据库',
    'Node labels': '节点类型',
    'Relationship types': '关系类型',
    'Example questions': '示例问题',
    'Raw Cypher': 'Cypher 示例',
    '⛁ Show the full plant graph': '⛁ 显示完整装置图谱',
    'every relief valve, via live Neo4j': '所有安全阀（经由实时 Neo4j）',
    'live Neo4j:': '实时 Neo4j：',
    '— raw Cypher runs against it': '— 原生 Cypher 将在其上执行',
    'live Neo4j: not running — plain-English questions still work (in-memory graph); raw Cypher needs':
      '实时 Neo4j：未运行 — 英文自然语言提问仍可用（内存图谱）；原生 Cypher 需先执行',
    'Ask the plant graph a question in plain English — every answer shows the equivalent':
      '用简单英文向装置图谱提问 — 每个回答都会给出等价的',
    ', the result': '、结果',
    'graph': '图形',
    'and a': '和',
    'table': '表格',
    'FLOWS_TO = verified flow direction · CONNECTED_TO = drawing order only. Click a node for its properties and one-click traces; pick an example on the left to start.':
      'FLOWS_TO = 已验证流向 · CONNECTED_TO = 仅为绘图顺序。点击节点可查看属性并一键追溯；请从左侧示例开始。',
    'List all relief valves': '列出所有安全阀',
    'List all vessels': '列出所有容器',
    'How many of each equipment type?': '各类设备数量统计',
    'Graph': '图形',
    'Table': '表格',
    'running…': '运行中…',
    'contracting…': '收缩计算中…',
    'copy Cypher': '复制 Cypher',
    'close frame': '关闭',
    'no rows': '无结果行',
    'show instruments': '显示仪表',
    'show unverified connections': '显示未验证连接',
    'isolated items form the strip at the bottom · scroll to zoom · click a node to explore from it':
      '孤立项排列于底部 · 滚轮缩放 · 点击节点可继续探索',
    'label': '类型',
    'name': '名称',
    'sheets': '图纸',
    'explore from here': '从此处探索',
    '↓ downstream': '↓ 下游',
    '↑ upstream': '↑ 上游',
    '⇄ neighbours': '⇄ 相邻设备',
    '⛑ relief path': '⛑ 泄放路径',

    /* LLM lab */
    'LLM Lab — which local model can replace the cloud generator?':
      'LLM 实验室 — 哪个本地模型能够替代云端生成器？',
    'Devices and candidate models are declared in': '设备与候选模型声明于',
    '(edit + commit; the page re-reads it on refresh, no restart). Each device is an Ollama server — start it with':
      '（编辑并提交；页面刷新时重新读取，无需重启）。每台设备是一个 Ollama 服务器 — 用',
    'on a trusted network so the hub can reach it. Every run (including failures — an out-of-memory':
      '在可信网络中启动，确保本机可访问。每次运行（包括失败 — 内存不足',
    'is': '也',
    'a result) lands in': '算一种结果）都会写入',
    'benchmarks:': '基准项:',
    'loading devices…': '设备加载中…',
    'Runs': '运行记录',
    '↻ refresh': '↻ 刷新',
    'no runs yet': '暂无运行记录',
    'busy': '忙碌',
    'Run benchmarks ▶': '运行基准测试 ▶',
    '⇩ pull': '⇩ 拉取',
    'on that machine:': '在该机器上：',
    ', then check the IP in the YAML.': '，然后核对 YAML 中的 IP。',
    'starting…': '启动中…',
    '✓ done': '✓ 完成',
    'when': '时间',
    'device': '设备',
    'model': '模型',
    'kind': '类别',
    'state': '状态',
    'done': '完成',
    'error': '错误',
    'tokens per s': '令牌/秒',
    'json schema ok': 'JSON 合规',
    'deviation coverage': '偏差覆盖率',
    'cause recall': '原因召回率',
    'hallucination rate': '幻觉率',
    'grounding precision': '溯源精确率',
    'latency p95 s': 'P95 延迟(秒)',
    'kind accuracy': '类别准确率',
    'intent accuracy': '意图准确率',
    'result accuracy': '结果准确率',
    'gold eval': '金标准评估',
    'throughput': '吞吐测试',
    'graph qa': '图谱问答',

    /* KB tab */
    'Hybrid retrieval (BM25 + dense, RRF fusion, guideword boost)':
      '混合检索（BM25 + 稠密向量 · RRF 融合 · 引导词加权）',
    'any guideword': '任意引导词',
    'any parameter': '任意参数',
    'NO': '无 (NO)', 'MORE': '过量 (MORE)', 'LESS': '过少 (LESS)',
    'REVERSE': '反向 (REVERSE)', 'AS_WELL_AS': '伴随 (AS WELL AS)',
    'PART_OF': '部分 (PART OF)', 'OTHER_THAN': '异常 (OTHER THAN)',
    'EARLY': '过早 (EARLY)', 'LATE': '过迟 (LATE)',
    'flow': '流量 (flow)', 'pressure': '压力 (pressure)',
    'temperature': '温度 (temperature)', 'level': '液位 (level)',
    'Search': '搜索',
    'Corpus & curation gates': '语料库与筛选门',
    'Only': '仅',
    'approved': '已批准',
    'documents are indexed; pending documents and the gold-eval holdout are excluded at ingestion (FR-AGM-2 / DDR-04).':
      '的文档才会被索引；待审文档与金标准评估留出集在入库时即被排除（FR-AGM-2 / DDR-04）。',
    'doc': '文档',
    'title': '标题',
    'type': '类型',
    'chunks': '分块',
    'gate': '筛选门',
    'approved — indexed': '已批准 — 已索引',
    'holdout — excluded': '留出集 — 已排除',
    'pending — excluded': '待审 — 已排除',
    'no applicable results (curation + applicability filters applied)':
      '无适用结果（已应用筛选与适用性过滤）',

    /* worksheet tab */
    'Run the Stage 3 reasoner': '运行第 3 阶段推理器',
    '2401 compressor train (real digitized P&ID)':
      '2401 压缩机组（真实数字化 P&ID）',
    'mock pump/vessel process (TK-100 → P-101 → V-201)':
      '模拟泵/容器流程（TK-100 → P-101 → V-201）',
    'Analyze node': '分析节点',
    'guideword × parameter matrix → KB retrieval → StubLLM → tag-grounding gate → worksheet (risk ranking left blank: human-only)':
      '引导词 × 参数矩阵 → 知识库检索 → StubLLM → 位号溯源校验门 → 工作表（风险分级留空：仅限人工）',
    'running reasoner…': '推理器运行中…',
    'deviation': '偏差',
    'causes': '原因',
    'consequences': '后果',
    'safeguards': '安全措施',
    'cause': '原因',
    'consequence': '后果',
    'safeguard': '安全措施',
    'UNSUPPORTED': '无佐证',
    'topology': '拓扑事实',
    'deterministic graph fact': '确定性图谱事实',
    'Grounding-gate audit': '溯源校验审计',
    '(rejected findings are kept, never silently dropped — MDL-10)':
      '（被拒发现全部保留，绝不静默丢弃 — MDL-10）',
    'no findings rejected on this run — hallucination rate 0%':
      '本次运行无发现被拒 — 幻觉率 0%',
    'invalid tags': '无效位号',
    'Completeness critic': '完整性评审',
    'Gold-standard evaluation': '金标准评估',
    '(mock node, deterministic)': '（模拟节点 · 确定性）',
    'retriever': '检索器',
    'verdict': '结论',
    'deviation coverage ≥85%': '偏差覆盖率 ≥85%',
    'cause recall ≥80%': '原因召回率 ≥80%',
    'PASS': '通过',
    'FAIL': '未通过',
    'MockRetriever (L3 baseline)': 'MockRetriever（L3 基线）',
    'Stage-2 KB (integrated)': '第 2 阶段知识库（集成）',
    'missed by the integrated KB': '集成知识库未命中项',
    'This gap is expected and honest: the Stage-2 corpus is curated for the 2401':
      '该差距是预期且如实呈现的：第 2 阶段语料库面向 2401',
    'air/nitrogen station': '空分/氮气站',
    '(compressors), while the gold set tests a mock':
      '（压缩机）进行筛选，而金标准测试集针对的是模拟',
    'pump': '泵',
    "process — pump-domain precedents (pump trip, cavitation/NPSH) aren't in the corpus yet. It closes by ingesting real historical HAZOP worksheets, which is the KB's planned build-out, not a reasoner defect.":
      '流程 — 泵相关先例（泵跳车、气蚀/NPSH）尚未入库。该差距将通过导入真实历史 HAZOP 工作表来弥合，属于知识库规划中的扩建，而非推理器缺陷。',

    /* §4 model gates */
    'Section 4.3 model-performance gates': '第 4.3 节 模型性能验收门',
    '(MDL-7 … MDL-13, one measured run)': '（MDL-7 … MDL-13 · 单次实测）',
    'Run scorecard': '运行评分卡',
    'run the scorecard…': '请运行评分卡…',
    'running one measured pass…': '正在执行一次实测…',
    'fabrication rate': '虚构率',
    'latency P95': 'P95 延迟',
    'omission detection': '遗漏检出率',
    'HUMAN AUDIT': '人工审核',
    'detail': '明细',
    'MDL-12 · per-deviation latency': 'MDL-12 · 单偏差延迟',
    '(full pipeline, serial — what a scribe waits for)':
      '（完整流程 · 串行 — 即记录员的实际等待时长）',
    'MDL-11 · expert audit sample': 'MDL-11 · 专家审计抽样',
    '(released citation-bearing suggestions; the <1% verdict is the human auditor\'s)':
      '（已发布的带引用建议；<1% 的判定由人工审计员作出）',
    'MDL-14 · accept/edit/reject telemetry': 'MDL-14 · 接受/编辑/拒绝遥测',
    '(append-only JSONL — use the ✓ ✎ ✗ buttons on the Worksheet tab to record events)':
      '（追加式 JSONL — 请在工作表页用 ✓ ✎ ✗ 按钮记录事件）',
    'sample': '样本',
    'claim': '论断',
    'citations (Stage B verdict)': '引用（B 阶段判定）',
    'supported': '有佐证',
    'unsupported': '无佐证',
    'no citation-bearing suggestions in this run': '本次运行无带引用的建议',
    'no events recorded yet — accept (✓), edit (✎) or reject (✗) suggestions on the Worksheet tab':
      '尚无事件记录 — 请在工作表页对建议执行接受（✓）、编辑（✎）或拒绝（✗）',
    'total events': '事件总数',
    'accepted → ai_generated_human_approved': '已接受 → ai_generated_human_approved',
    'edited → ai_generated_human_modified': '已编辑 → ai_generated_human_modified',
    'rejected (removed from body, kept in audit trail)':
      '已拒绝（移出正文，保留于审计记录）',

    /* RTM */
    'Requirements Traceability Matrix': '需求追踪矩阵',
    '(Fable §9 — every FR/AR/MDL/DR/NFR/VV item, status is human judgment, code citations auto-scanned)':
      '（Fable 第 9 章 — 全部 FR/AR/MDL/DR/NFR/VV 条目；状态为人工判定，代码引用自动扫描）',
    'all': '全部',
    'partial': '部分完成',
    'todo': '待办',
    'blocked': '受阻',
    'out_of_scope': '范围外',
    'id': 'ID',
    'requirement': '需求',
    'status': '状态',
    'notes': '备注',
    'evidence': '证据',
    'no code citations': '无代码引用',
    'text': '内容',
    'downstream': '下游',
    'upstream': '上游',
  };
  // standalone equipment-type words (legend pills, table cells) fall back
  // to the glossary unless EXACT already has a more specific meaning
  for (const [k, v] of Object.entries(GLOSS)) {
    if (!(k in EXACT)) EXACT[k] = v;
  }

  /* ---------------- pattern rules (interpolated strings) ------------- */
  /* Order matters: specific before generic. [regex, string-or-fn]      */
  const PATTERNS = [
    [/^valves \(incl\. (\d+) PSV, (\d+) check\)$/, '阀门（含安全阀 $1、止回阀 $2）'],
    [/(\d+) nodes · (\d+) connections · (\d+) with verified flow direction/,
     '$1 个节点 · $2 条连接 · 其中 $3 条流向已验证'],
    [/(\d+) row\(s\) from live Neo4j/, '实时 Neo4j 返回 $1 行'],
    [/^(\d+) items? (downstream|upstream) of ([\w-]+) — (\d+) over fully verified flow, (\d+) reachable only across unverified connections\.?$/,
     (m, n, dir, tag, v, u) => tag + ' ' + (dir === 'downstream' ? '下游' : '上游')
       + '共 ' + n + ' 项 — 其中 ' + v + ' 项经完全验证的流向可达，'
       + u + ' 项仅经未验证连接可达。'],
    [/(\d+)\/(\d+) pipe runs directed/g, '$1/$2 条管线已定向'],
    [/(\d+) off-page connectors/g, '$1 个跨图连接符'],
    [/(\d+) nodes\b/g, '$1 个节点'],
    [/(\d+) edges\b/g, '$1 条边'],
    [/(\d+) relationships\b/g, '$1 条关系'],
    [/(\d+) deviations, (\d+) findings/, '$1 个偏差 · $2 条发现'],
    [/\bmembers:/, '成员:'],
    [/(\d+) graph-accuracy failure\(s\)/, '$1 个图谱问答失败样例'],
    [/weighted progress over (\d+) requirements —/, '加权进度 · 共 $1 项需求 —'],
    [/^cited ×(\d+)$/, '已引用 ×$1'],
    [/^sheet (\d+)$/, '图纸 $1'],
    [/^target (.+)$/s, '目标 $1'],
    [/^missing: /, '缺失：'],
    [/^missed: /, '未命中：'],
    [/^unreachable: /, '无法连接：'],
    [/^confidence ([\d.]+)$/, '置信度 $1'],
    [/ — not installed/g, ' — 未安装'],
    [/\(critic\)/g, '（评审模型）'],
    [/gold_eval runs all 29 deviations through the model[\s\S]*critic model:\s*(\S+)/,
     'gold_eval 将全部 29 个偏差送入模型 — 耗时数分钟而非数秒 · 评审模型：$1'],
    [/\b(arrow|check-valve|connector|conservation|propagated) (\d+)/g,
     (m, w, n) => ({'arrow': '箭头', 'check-valve': '止回阀',
                    'connector': '连接符', 'conservation': '守恒推断',
                    'propagated': '传播推断'}[w] + ' ' + n)],
    /* deviation labels — standalone, and cited inside [brackets] */
    [/^(No\/Not|More|Less|Reverse|As well as|Part of|Other than|Early|Late) (Flow|Pressure|Temperature|Level)$/,
     (m, gw, p) => devZh(gw, p) || m],
    [/\[(No\/Not|More|Less|Reverse|As well as|Part of|Other than|Early|Late) (Flow|Pressure|Temperature|Level)\]/g,
     (m, gw, p) => '[' + (devZh(gw, p) || gw + ' ' + p) + ']'],
    /* example questions (visible text only — data-q stays English) */
    [/^What is downstream of (.+)\?$/, '$1 的下游是什么？'],
    [/^What is upstream of (.+)\?$/, '$1 的上游是什么？'],
    [/^What feeds (.+)\?$/, '$1 的进料来自哪里？'],
    [/^Does (.+) have a relief path\?$/, '$1 是否有泄放路径？'],
    [/^What is connected to (.+)\?$/, '$1 与什么相连？'],
    [/^Show the path from (.+) to (.+)$/, '显示从 $1 到 $2 的路径'],
    [/^Which vessels are downstream of (.+)\?$/, '哪些容器位于 $1 下游？'],
    [/^list all ([a-z_ ]+?)s$/,
     (m, w) => GLOSS[w.trim()] ? '列出所有' + GLOSS[w.trim()] : m],
    /* CamelCase node-label pills, e.g. "ReliefValve (5)" */
    [/^([A-Za-z][A-Za-z_ -]*) \((\d+)\)$/,
     (m, w, n) => {
       const k = w.replace(/([a-z])([A-Z])/g, '$1 $2').toLowerCase();
       return GLOSS[k] ? GLOSS[k] + '（' + n + '）' : m;
     }],
    /* status chips with counts, kind chips, type pills */
    [/^(done|partial|todo|blocked|out_of_scope) (\d+)$/,
     (m, s, n) => (EXACT[s] || s) + ' ' + n],
    [/^● (compatible|degraded|unsupported)$/,
     (m, v) => '● ' + ({compatible: '兼容', degraded: '部分兼容',
                        unsupported: '不兼容'}[v])],
    [/^([a-z][a-z_ -]*) \((\d+)\)$/,
     (m, w, n) => GLOSS[w] ? GLOSS[w] + '（' + n + '）' : m],
    [/^([a-z][a-z_-]*) (\d+)$/,
     (m, w, n) => GLOSS[w] ? GLOSS[w] + ' ' + n : m],
    [/^(accepted|edited|rejected) \(MDL-14 telemetry\)$/,
     (m, a) => ({accepted: '接受', edited: '编辑',
                 rejected: '拒绝'}[a]) + '（MDL-14 遥测）'],
    /* overview narrative paragraph (contains the node count) */
    [/The vector P&ID is digitized deterministically[\s\S]*tabs above\./,
     (m) => {
       const n = (m.match(/(\d+)-node/) || [, '—'])[1];
       return '矢量 P&ID 以确定性方式（无机器学习、无 OCR）数字化为带类型的拓扑图。' +
         '知识层将约 3000 个几何节点收缩为 ' + n + ' 个节点的设备图谱，' +
         '在每条连接上携带流向置信度，并通过混合检索器提供经人工筛选的安全知识。' +
         '推理器对每个分析节点遍历完整偏差矩阵，将每条生成的发现与图谱做溯源校验，' +
         '最终输出可审计的工作表。请通过上方标签页浏览各阶段。';
     }],
  ];

  /* ---------------- attribute translations (placeholder/title) ------- */
  const ATTRS = ['placeholder', 'title'];
  const EXACT_ATTR = {
    'ask in plain English — "what is downstream of 2401-K-001A?" — or paste Cypher (MATCH …)':
      '请用英文提问 — 如 "what is downstream of 2401-K-001A?" — 或粘贴 Cypher（MATCH …）',
    'e.g. reverse flow compressor trip':
      '请用英文关键词，如 reverse flow compressor trip',
    'filter… e.g. FR-ARE, telemetry, simulator':
      '筛选… 如 FR-ARE、telemetry',
    'pull: ollama name or hf.co/user/repo-GGUF':
      '拉取：ollama 模型名或 hf.co/user/repo-GGUF',
    'copy Cypher': '复制 Cypher',
    'close frame': '关闭',
  };

  /* ---------------- engine ------------------------------------------- */
  const norm = (s) => s.trim().replace(/\s+/g, ' ');
  const textOrig = new WeakMap();   // text node -> original nodeValue
  const attrOrig = new WeakMap();   // element  -> {attr: original}

  function zhOf(raw) {
    const key = norm(raw);
    if (!key) return null;
    const lead = raw.match(/^\s*/)[0], trail = raw.match(/\s*$/)[0];
    if (EXACT[key] !== undefined) return lead + EXACT[key] + trail;
    // run patterns on the trimmed body so ^/$ anchors survive the
    // indentation that template literals leave around text nodes
    let s = raw.slice(lead.length, raw.length - trail.length), hit = false;
    for (const [re, rep] of PATTERNS) {
      const t = s.replace(re, rep);
      if (t !== s) { s = t; hit = true; }
    }
    return hit ? lead + s + trail : null;
  }

  function skip(el) {
    if (!el) return true;
    if (el.closest('script,style,code,pre,.fq,.fcypher')) return true;
    // an <option> without a value attribute submits its text — leave it
    if (el.tagName === 'OPTION' && !el.hasAttribute('value')) return true;
    return false;
  }

  function walk(root) {
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    let n;
    while ((n = w.nextNode())) nodes.push(n);
    if (root.nodeType === Node.TEXT_NODE) nodes.push(root);
    for (const t of nodes) {
      if (skip(t.parentElement)) continue;
      if (lang === 'zh') {
        const raw = textOrig.has(t) ? textOrig.get(t) : t.nodeValue;
        const out = zhOf(raw);
        if (out !== null) {
          if (!textOrig.has(t)) textOrig.set(t, raw);
          if (t.nodeValue !== out) t.nodeValue = out;
        }
      } else if (textOrig.has(t)) {
        t.nodeValue = textOrig.get(t);
      }
    }
    if (root.querySelectorAll) {
      const els = root.querySelectorAll('[placeholder],[title]');
      for (const el of els) translateAttrs(el);
      if (root.getAttribute) translateAttrs(root);
    }
  }

  function translateAttrs(el) {
    for (const a of ATTRS) {
      if (!el.getAttribute) continue;
      const cur = el.getAttribute(a);
      if (cur === null) continue;
      const saved = attrOrig.get(el) || {};
      if (lang === 'zh') {
        const raw = a in saved ? saved[a] : cur;
        const out = EXACT_ATTR[norm(raw)] !== undefined
          ? EXACT_ATTR[norm(raw)] : zhOf(raw);
        if (out !== null && out !== cur) {
          if (!(a in saved)) { saved[a] = raw; attrOrig.set(el, saved); }
          el.setAttribute(a, out);
        }
      } else if (a in saved && cur !== saved[a]) {
        el.setAttribute(a, saved[a]);
      }
    }
  }

  /* ---------------- language state + toggle --------------------------- */
  const TITLE_EN = document.title;
  let lang = localStorage.getItem('hazop-lang');
  if (lang !== 'zh' && lang !== 'en') {
    lang = (navigator.language || '').toLowerCase().startsWith('zh')
      ? 'zh' : 'en';
  }

  let btn = null;
  function applyLang() {
    document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';
    document.title = lang === 'zh'
      ? (EXACT[norm(TITLE_EN)] || TITLE_EN) : TITLE_EN;
    if (document.body) walk(document.body);
    if (btn) btn.textContent = lang === 'zh' ? 'EN' : '中文';
  }
  function setLang(l) {
    lang = l;
    localStorage.setItem('hazop-lang', l);
    applyLang();
  }

  new MutationObserver((muts) => {
    if (lang !== 'zh') return;
    for (const m of muts) {
      for (const n of m.addedNodes) {
        if (n.nodeType === Node.ELEMENT_NODE ||
            n.nodeType === Node.TEXT_NODE) walk(n);
      }
    }
  }).observe(document.documentElement, {childList: true, subtree: true});

  document.addEventListener('DOMContentLoaded', () => {
    btn = document.createElement('button');
    btn.id = 'lang-toggle';
    btn.style.cssText = 'position:fixed;top:12px;right:18px;z-index:99;' +
      'background:#1a1a20;border:1px solid #3a3a44;color:#4fc3f7;' +
      'border-radius:6px;padding:6px 14px;cursor:pointer;font-size:13px';
    btn.onclick = () => setLang(lang === 'zh' ? 'en' : 'zh');
    document.body.appendChild(btn);
    applyLang();
  });
})();
