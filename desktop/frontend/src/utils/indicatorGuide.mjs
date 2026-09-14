// Describes Stock King's local calc.ts implementation, not a promise of returns.
const row = (key, name, group, read, formula, caution = '', aliases = []) => ({ key, name, group, read, formula, caution, aliases })
export const INDICATORS = [
  row('MA','MA 均线','趋势','先看均线方向，再看价格位置；5 > 10 > 20 > 60 为本页多头排列。','MA(N) = 最近 N 根收盘价之和 / N；图上为 5 / 10 / 20 / 60。','交叉有滞后，横盘会反复。'),
  row('EMA','EMA 指数均线','趋势','12 线在 21 线上方表示短期均价较强；配合趋势斜率。','EMA = α×C + (1−α)×前值，α=2/(N+1)；先以 N 根 SMA 初始化。'),
  row('BOLL','BOLL 布林带','波动','看收缩、扩张及价格在通道的位置；触上轨也可能延续趋势。','中轨=SMA20(C)；上/下轨=中轨±2σ20，σ 用总体标准差（除以20）。','越轨不是自动反转，也不是已确认突破。'),
  row('MACD','MACD 动量','动量','先看零轴，再看 DIF/DEA 关系与柱体变化；本页多头要求 DIF>DEA 且柱>0。','DIF=EMA12(C)−EMA26(C)；DEA=EMA9(DIF)；柱=2×(DIF−DEA)。','本软件柱体乘2；与未乘2的平台数值不同。'),
  row('RSI','RSI 相对强弱','动量','50 区分强弱；70/30 表示偏热/偏冷。极值可能持续，不能直接当卖点/买点。','RSI14=100−100/(1+Σ14上涨幅度/Σ14下跌幅度)。','本地采用滚动窗口，非 Wilder 平滑；无下跌时返回100（含平盘窗口）。'),
  row('KDJ','KDJ 随机指标','动量','看 K/D 交叉与所处区间；20/80 附近要结合趋势确认。','RSV=100×(C−LL9)/(HH9−LL9)；K=(2K前+RSV)/3；D=(2D前+K)/3；J=3K−2D，初值50。','标签含超卖偏多/超买偏空规则；并不表示已发生反转。'),
  row('OBV','OBV 能量潮','量能','价格创新高时，观察 OBV 是否同步；背离只提供复核线索。','初值=首根成交量；随后 OBV=前值+sign(C−C前)×V。','不是实际资金净流入。'),
  row('ATR','ATR 真实波幅','波动','数值上升表示波动变大，用于衡量风险距离，不指示涨跌。','TR=max(H−L,|H−C前|,|L−C前|)；ATR14 首值为均值，后续=(13×前ATR+TR)/14。'),
  row('VWAP','VWAP 量价均线','量能','看价格相对滚动成交均价的位置，再用量能确认。','TP=(H+L+C)/3；VWAP20=Σ20(TP×V)/Σ20V。','这是20根滚动均价，不是开盘以来的日内锚定 VWAP。'),
  row('MFI','MFI 资金流量','量能','50 看相对强弱；20/80 的极值应结合价格和趋势。','MF=TP×V；按TP变化分正负，MFI14=100−100/(1+Σ正MF/Σ负MF)。','量价构造值，不等于真实主力资金。'),
  row('KAMA','KAMA 自适应均线','趋势','价格在上方且线向上偏强；震荡中需防止来回穿越。','ER=|C−C10前|/Σ10|ΔC|；SC=[ER×(2/3−2/31)+2/31]²；KAMA=前值+SC×(C−前值)。'),
  row('Keltner','Keltner 通道','波动','以趋势中线和 ATR 通道观察扩张；结合成交量核对突破。','中线=EMA20(C)；上下轨=中线±1.5×ATR10。'),
  row('Supertrend','SuperTrend 趋势线','趋势','观察趋势线的翻转，再看价格是否持续站稳对应一侧。','基础带=(H+L)/2±3×ATR10；按前收盘与前带值递推止损带，越带翻转。','翻转是跟踪规则，有滞后，不能保证按线价成交。'),
  row('Ichimoku','Ichimoku 一目均衡','趋势','价格在云外且转换线/基准线方向一致时再观察延续；云内偏混合。','转换线=(HH9+LL9)/2；基准线=(HH26+LL26)/2；云A=(转换+基准)/2，云B=(HH52+LL52)/2，前移26根。','向右绘制是位移显示，不是未来价格预测。'),
  row('CCI','CCI 顺势指标','动量','超过 +100 或低于 −100 表示偏离均值较大；先确认市场是趋势还是横盘。','CCI20=(TP−SMA20(TP))/(0.015×20根平均绝对偏差)。'),
  row('TTMSqueeze','TTM 波动挤压','波动','先找 BOLL 收入 Keltner 的挤压，再看解除时动量方向。','挤压=BOLL(20,2)位于Keltner(20,10,1.5)内；本地动量=TP−EMA20(TP)。','本地简化版；动量不是原版线性回归计算。',['TTM']),
  row('SAR','SAR 抛物线','趋势','价格相对 SAR 点及翻转用于跟踪趋势；窄幅震荡容易反复。','SAR=前SAR+AF×(EP−前SAR)；AF从0.02递增至0.2，并受前两根高低点约束。'),
  row('Donchian','Donchian 唐奇安','波动','比较价格与前一根已完成通道的边界，配合量能检查突破。','上轨=HH20；下轨=LL20；中轨=(上+下)/2。','本页标签比较含当前K线的通道边界，触轨仅为规则状态。'),
  row('ADX','ADX 趋势强度','趋势','ADX>25 表示趋势较强；方向需看 +DI 与 −DI，不能只看 ADX。','±DI=100×平滑(±DM)/平滑TR；DX=100×|+DI−−DI|/(+DI+−DI)；ADX14=DX的Wilder平滑。'),
  row('WilliamsR','W%R 威廉指标','动量','−80/−20 提示偏冷/偏热；等待价格确认，避免逆强趋势。','W%R14=−100×(HH14−C)/(HH14−LL14)。','本页超卖标偏多、超买标偏空，不表示反转已确认。',['W%R','Williams%R']),
  row('StochRSI','StochRSI 随机强弱','动量','低位 K 上穿 D 仅是观察条件；须配合趋势，避免重复叠加 RSI。','Raw=100×(RSI14−LL14(RSI))/(HH14(RSI)−LL14(RSI))；K=SMA3(Raw)，D=SMA3(K)。'),
  row('CMF','CMF 量价流向','量能','零轴上/下表示收盘更偏成交区间上/下部；观察持续性。','CMF20=Σ20[(2C−H−L)/(H−L)×V]/Σ20V。','本页用±0.05区分偏多/偏空；不是账户资金流。'),
  row('Aroon','Aroon 趋势时效','趋势','Up 高、Down 低说明近期高点更近；反之偏弱。','Up=100×(25−距25根内最高点根数)/25；Down同理使用最低点。'),
  row('CMO','CMO 动量摆动','动量','正负看涨跌幅度差，±50 表示较强偏向。','CMO14=100×(Σ上涨−Σ下跌)/(Σ上涨+Σ下跌)。'),
  row('ForceIndex','FI 劲道','量能','零轴方向结合趋势看量价推动；放量跳空可能造成尖峰。','FI13=EMA13((C−C前)×V)。','不是独立的资金流证据。',['FI']),
  row('Pivot','Pivot 枢轴','结构','把 R/S 作为观察位置，再看成交与价格反应。','PP=(H前+L前+C前)/3；R1=2PP−L前；S1=2PP−H前；R2/S2=PP±(H前−L前)。','使用前一根K线，不固定为前一交易日。'),
  row('DEMA','DEMA 双重均线','趋势','观察价格位置与线的斜率；比普通均线更敏感，也更易反复。','E1=EMA21(C)；E2=EMA21(E1)；DEMA=2E1−E2。'),
  row('ZigZag','ZigZag 波段','结构','整理已形成波段；末段只作暂定结构，等待足够反向幅度确认。','追踪高低极值，反向幅度达到5%后转为下一段。','末端会重绘，回看图上的拐点不等于当时可知。'),
  row('SATS','SATS 自适应趋势','趋势','结合方向线、波动和趋势质量看持续性；属于自定义规则。','核心：TQI=0.35×ER+0.20×量能+0.25×结构+0.20×动量；围绕HL2构造自适应ATR带。','这里只展示主干；实现另含平滑、非对称带与翻转条件，无已验证胜率。'),
  row('AvgAmp','平均振幅','波动','比较5/10/20根平均振幅，观察波动是否扩大。','SMA5/10/20(振幅%)；优先使用行情振幅，缺失时以100×(H−L)/O替代。','替代口径与按昨收计算的振幅不同。'),
  row('Alligator','Alligator 鳄鱼线','趋势','三线发散且顺序一致用于观察趋势，纠缠时慎读交叉。','本地用SMA13/8/5(HL2)，分别右移8/5/3根。','本地为SMA，不是常见的SMMA版本。'),
  row('AO','AO 动量','动量','看零轴和柱体增减；与价格趋势相互核对。','AO=SMA5(HL2)−SMA34(HL2)。'),
  row('HullMA','HullMA 赫尔均线','趋势','斜率变化提供敏感的趋势线索；用量能过滤单根扰动。','HMA9=WMA√9(2×WMA⌊9/2⌋(C)−WMA9(C))。'),
  row('AD','A/D 累积派发','量能','看累计线方向与价格是否一致；背离须补证。','A/D=前值+[(2C−H−L)/(H−L)]×V。','量价估计，不是实际持仓变化。',['A/D']),
  row('TRIX','TRIX 三重动量','动量','观察 TRIX 与信号线的相对位置，再核对零轴。','E3=EMA15(EMA15(EMA15(C)))；TRIX=100×(E3/E3前−1)；信号线=EMA9(TRIX)。'),
  row('TRIXSlope','TRIX 斜率','动量','大于零表示 TRIX 加速向上，小于零表示向下；不等于价格必涨跌。','斜率=TRIX15当前−TRIX15前值。','',['TRIX斜率']),
  row('ROC','ROC 变动率','动量','比较当前与12根前收盘价；留意基期异常造成的变化。','ROC12=100×(C/C12前−1)。'),
  row('Fractal','Fractal 分形','结构','以已确认分形观察局部高低点，再结合趋势位置。','中间K线高点高于左右各2根为顶分形，低点低于左右各2根为底分形。','需要右侧2根确认，标记画回中心K线，不能作为当时已知入场点。'),
  row('CHOP','CHOP 震荡指数','波动','高值偏震荡，低值偏趋势；方向另外看均线。','CHOP14=100×log10(Σ14TR/(HH14−LL14))/log10(14)。'),
  row('ElderRay','ElderRay 多空力量','动量','比较最高/最低价相对 EMA 的距离，结合趋势和变化读。','Bull=H−EMA13(C)；Bear=L−EMA13(C)。','标签是简化的顺序判定，不能视作多空资金测量。'),
  row('ChaikinOsc','Chaikin 振荡','量能','零轴及斜率反映 A/D 的短长差，需价格确认。','Chaikin=EMA3(A/D)−EMA10(A/D)。'),
  row('VWAPBands','VWAP 量价通道','波动','观察价格相对成交加权均价及偏离范围。','VWAP20±2×成交量加权标准差；TP=(H+L+C)/3。','滚动20根口径；越轨不保证回归。'),
  row('MassIndex','Mass Index 梅斯','波动','27 附近的回落提示波幅结构变化，方向需另查趋势。','Mass=Σ25[EMA9(H−L)/EMA9(EMA9(H−L))]。','旧规则下穿27标为偏多；该指标本身不判断反转方向。'),
  row('UlcerIndex','Ulcer 回撤','波动','数值越大表示窗口内回撤越深，用于风险比较。','窗口最高收盘价=P；UI14=√mean14([100×(C/P−1)]²)。','本地同一窗口共用最高价；低回撤标签不等于未来上涨。'),
  row('Coppock','Coppock 估波','动量','看零轴及拐头，周期长度取决于当前K线。','Coppock=WMA10(ROC14+ROC11)。','日线上的14是14根日K，不是14个月。'),
  row('TEMA','TEMA 三重均线','趋势','价格与均线关系配合斜率阅读，敏感性更高。','E1=EMA21(C)，E2=EMA21(E1)，E3=EMA21(E2)；TEMA=3E1−3E2+E3。'),
  row('TEMASlope','TEMA 斜率','动量','柱体表示均线变化，平滑线辅助观察加速与减速。','Raw=TEMA21当前−前值；平滑线=EMA5(Raw)。','斜率为价格差值，跨股票不宜直接比较。',['TEMA斜率']),
  row('SMI','SMI 动量指数','动量','零轴、主线与信号线的关系辅助读动量。','Raw=200×[C−(HH14+LL14)/2]/(HH14−LL14)；SMI=EMA3(Raw)，信号=EMA3(SMI)。','本地简化版，并非双重平滑分子分母的经典SMI。'),
  row('SignalRatio','信号占比','综合','显示各项可计算规则标签的数量比例；先读冲突原因。','类别占比=该类规则数/全部可计算规则数×100%。','包含未画出的指标；多个同源指标相关，不是投票胜率。'),
  row('SMC','SMC 市场结构','结构','结合已确认摆动点观察结构突破与反转，再核对成交。','使用内部5根/摆动50根等结构窗口，基于摆动高低点识别BOS、CHoCH等。','摆动点依赖后续确认；图上回溯标记不是提前预测。'),
  row('Chip','筹码分布','量能','观察估计的成交价格密集区，作为价格位置线索。','根据历史量价与分布假设估计各价格区间的权重。','不是投资者真实持仓、成本或可验证的主力仓位。'),
]
export const COMBINATIONS = [
  { id:'trend', name:'趋势确认', keys:['MA','MACD','OBV'], steps:'先看 MA 方向 → 看 MACD 动量是否同向 → 用 OBV 核对量能。', caution:'均线与 MACD 都来自价格，并非两份独立证据。' },
  { id:'range', name:'区间观察', keys:['BOLL','RSI','ADX'], steps:'先用 ADX 判断趋势强弱 → 看 BOLL 位置 → 等 RSI 与价格共同确认。', caution:'强趋势中不因触轨或超买超卖而直接反向操作。' },
  { id:'breakout', name:'突破核对', keys:['Donchian','OBV','ATR'], steps:'先比较前一根通道边界 → 看 OBV 是否同步 → 用 ATR 估计波动距离。', caution:'还需核对成交、滑点和假突破；ATR不判断方向。' },
  { id:'flow', name:'量价复核', keys:['VWAP','CMF','MA'], steps:'先看 MA 趋势 → 看价格相对滚动 VWAP → 看 CMF 持续性。', caution:'VWAP和CMF均由量价计算，不是真实资金账户数据。' },
]
const normalize = value => String(value || '').replace(/^show/,'').replace(/[\s/]/g,'').toLowerCase()
export function indicatorFor(value) { const id=normalize(value); return INDICATORS.find(item => [item.key,item.name,...item.aliases].some(alias=>normalize(alias)===id)) }
export function combinationsFor(value) {
  const item=indicatorFor(value); if(!item)return []
  const direct=COMBINATIONS.filter(combo=>combo.keys.includes(item.key))
  if(direct.length)return direct
  const support=item.group==='量能'?['MA','ATR']:item.group==='波动'?['MA','OBV']:['OBV','ATR']
  return [{ id:`with-${item.key}`, name:'互补观察', keys:[item.key,...support.filter(key=>key!==item.key)], steps:`先读 ${item.name} 的条件 → 用 ${support.join(' / ')} 核对量能、趋势或波动。`, caution:'这是阅读顺序示例，未经策略回测，不提供胜率。' }]
}
export function indicatorTip(value) { const item=indicatorFor(value); return item ? `${item.name}\n${item.read}\n搭配：${combinationsFor(value)[0]?.keys.join(' + ')}\n公式：${item.formula}${item.caution ? '\n注意：'+item.caution : ''}` : '暂无说明' }
