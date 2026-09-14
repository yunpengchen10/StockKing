"""Build the same illustrated Stock King guide as Markdown and a typeset PDF."""
from pathlib import Path
from html import escape
import json, re, zipfile
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.colors import HexColor, white
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph, Table, TableStyle
from reportlab.lib.utils import ImageReader
from pypdf import PdfReader

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'output/pdf/Stock-King-v2.4.2-图文说明书'
OUT.mkdir(parents=True,exist_ok=True)
TITLE='Stock King 应用介绍与图文使用说明'
BASE='Stock-King-v2.4.2-图文说明书'

def p(text): return {'type':'p','text':text}
def h(text): return {'type':'h','text':text}
def note(text): return {'type':'note','text':text}
def pic(file,caption,height=220): return {'type':'image','file':file,'caption':caption,'height':height}
def table(headers,rows,widths=None): return {'type':'table','headers':headers,'rows':rows,'widths':widths}
def steps(items): return {'type':'steps','items':items}
def cols(left,right): return {'type':'cols','left':left,'right':right}

PAGES=[
 {'title':'认识 Stock King','sub':'把找线索、看证据、做研究与记计划放在同一处。','blocks':[
  p('Stock King 是面向个人投资研究的桌面工作台。它把自选、精选、K线、AI研究、策略验证与投资记录连接起来，让每次判断都能回到来源、条件和记录。'),
  pic('01-native-picks.png','图1｜已安装版实拍：精选卡片及依据与风险。2026-09-08界面快照，画面股票不构成本书推荐。',205),
  table(['入口','主要用途','本书页码'],[['精选','发现候选，追溯入选依据','03-04'],['图表 / 多图','观察价格、指标与相同周期下的差异','05-06'],['AI 研究','运行已有API，或导入自己的ChatGPT结果','07-10'],['投资 / 策略','记计划与成交；查看模型、回测及验证状态','11'],['自选 / 市场 / 工具','管理观察列表；浏览行情与其他研究功能','按页面入口使用']],[85,345,81]),
  note('阅读路线：精选 → 图表 → AI复核 → 建计划。主界面保留简短操作，详细说明通过“为何入选”“指标”“使用说明”等入口展开。'),
  p('适用版本：Windows x64，Stock King v2.4.2。图表可研究A股、港股、美股；当前精选、AI证据工作区及人民币仓位预算重点支持A股。红色表示上涨，绿色表示下跌。'),
 ]},
 {'title':'精选：先看时间，再看原因','sub':'榜单给出待研究对象；下一步是复核，不是直接下单。','blocks':[
  cols([pic('02a-pick-card.png','图2｜卡片局部：演示数据。',245)], [
   steps([('重扫','等引擎就绪后点“重扫”。先核对Top 5旁的生成时间，缓存或历史结果不等于实时行情。'),('为何入选','点击卡片的入选原因，查看实际特征、评分拆分、数据来源及缺失项。'),('继续研究','“图表”核对当前K线；“AI复核”带入这次精选快照；“自选”加入观察列表。'),('建计划','核对条件后，记录触发、失效、预算与复核日期。建计划不会替你向券商下单。')])
  ]),
  h('榜单的两个视角'),
  table(['栏目','应该怎样使用'],[['当日机会','规则研究榜，最多5只。先做硬过滤，再按分支、门槛和候选池约束选择；允许不足5只或空榜。'],['策略分组','保守、均衡、进攻使用不同目标，分数不能跨组直接比较。未发布模型应按规则观察理解。']],[92,419]),
  p('“历史”可以回看过去的快照；看到“历史快照”时，可点“返回最新”。“后台学习”控制后台定时扫描与历史反馈，不等于实时推送，也不代表点击开关后模型立即成熟。'),
  note('100分也不是100%胜率。“重扫”返回缓存时，后台可能仍在更新；始终检查生成时间、当下价格和可成交条件。'),
 ]},
 {'title':'为何入选：让分数有依据','sub':'先理解所属分支，再查看支持它的具体证据。','blocks':[
  cols([
   h('分支速查'),
   table(['分支','主要线索'],[['M1','事件：并购、重组、控制权等文本线索'],['M2','题材：政策、产业等文本线索'],['M3','量价：成交额倍数、量比与换手'],['M4','突破：近期涨停特征与突破幅度'],['M5','经营：订单、中标、业绩等线索'],['M6-A','二阶段修复'],['M6-B','强波动下的区间修复']],[42,201]),
   p('这是软件的规则分类，不是七个独立胜率模型。文本命中需要回原始公告核验；“涨停特征次数”来自涨幅阈值，不宜当成完整逐笔涨停记录。'),
   p('卡片使用当前分支分；14:55快照改用NLS尾盘研究分。NLS包含量价、尾盘强度、题材/催化代理项等，并非次日收益预测。'),
  ],[pic('03-pick-reason.png','图3｜特征与来源抽屉，演示数据。',425)]),
  h('在抽屉里检查这四件事'),
  p('① 特征是否真实提供；② 评分属于哪个分支/时点；③ 来源与提取时间是否能追溯；④ 确认、失效和数据缺口是否清楚。提取时间不是新闻的原始发布时间。'),
  note('历史频率需20个已成熟官方交易日、60日行情及30个有效相似案例；未达到门槛仍可研究候选，但不能用研究分代替概率。'),
 ]},
 {'title':'图表：把指标真正画出来','sub':'从精选进入图表，也可以在左侧“图表”搜索名称或代码。','blocks':[
  pic('11-chart.png','图4｜生产界面演示：MA已叠加到主图，MACD显示在副图；行情为演示数据。',325),
  steps([('选择周期与复权','先确认日K、周K或分钟周期，以及前复权、后复权或不复权。比较前后结果时保持同一口径。'),('点击 MA 等快捷按钮','MA、BOLL、MACD、RSI、VWAP可直接开关。“✓”表示开启；MA5/10/20/60会在图上标出。'),('打开“指标”','查找其他指标，选中后点“画到图上”；再次点“已画·隐藏”关闭。多图也有同样入口。')]),
  note('图上的“多单”“开仓”“止损”“止盈”等是研究标线与测量工具，不是已成交订单。最新一根K线在收盘前仍可能变化。'),
 ]},
 {'title':'指标信号：读条件，也读冲突','sub':'悬浮快速了解，点击展开公式与组合。','blocks':[
  cols([pic('12-indicator.png','图5｜MA解读面板，演示行情。',455)],[
   steps([('悬浮或点击信号','在信号名称上悬浮看简要说明；点击或按Enter打开详细面板。留意面板显示的K线日期。'),('按需叠加组合','例如“MA + MACD + OBV”：先看趋势方向，再看动量，最后核对量能。组合按钮只增加指标，保留其他已开启项。'),('展开公式与口径','N表示当前周期的N根K线，不一定是N天。以本软件面板公式为准。')]),
   h('两个容易误读的地方'),
   p('信号占比统计所有可计算的规则标签，包括未画出的指标。多个指标可能同源相关，不能把占比当作投票胜率。'),
   p('RSI采用本地滚动窗口；VWAP采用20根滚动量价均值；MACD柱为2×(DIF-DEA)。与其他平台数值不同，先核对口径。'),
  ]),
  note('组合是阅读示例，未经收益回测。分形需要右侧K线确认，ZigZag末端会更新；回看图上的拐点不代表当时已经知道。'),
 ]},
 {'title':'AI 分析一：使用已有 API','sub':'选股票、选模板、选平台与模型，再主动运行。','blocks':[
  pic('06-ai-controls.png','图6｜API研究操作区。平台和模型名称为演示占位，未调用真实AI服务。',175),
  steps([('配置一次，之后复用','进入“工具 → AI平台配置”，添加配置名称、接口地址、令牌和模型名称，确认后点“保存配置”。设置页也可跳转到该页面。'),('选择股票与模板','在AI研究选择A股；从精选点“AI复核”会带入选中快照，并默认选择“反方审查”。也可换成短线、波段或其他模板。'),('明确选择平台和模型','平台下拉框来自软件已有配置；再选择该平台下可用的模型配置。显示配置不完整时先补齐，不会默认替你选另一个平台。'),('运行与复核','点“运行研究”后查看报告、来源和量化对照。需要时先点“更新证据”。运行中可取消，但已发出请求仍按平台规则计费。')]),
  note('API调用使用你配置的平台和密钥。ChatGPT订阅不能代替这里的API配置；不想用API，可按下一页生成研究包并手动导入结果。'),
 ]},
 {'title':'AI 分析二：使用自己的 ChatGPT','sub':'把研究包交给ChatGPT，再把结果带回Stock King。','blocks':[
  pic('08-export.png','图7｜生成研究包后可复制或下载。示例数据与选中快照会作为证据单独保留。',185),
  steps([('生成研究包','选股票和模板，切换“研究包”，点“生成研究包”。软件会整理已有证据、来源与时点。'),('在自己的会话中分析','复制或下载研究包，手动交给自己的ChatGPT。让它按包内格式回答；不足的证据应明确写成未知。'),('返回“导入报告”','粘贴结果，或导入TXT、MD、JSON；填写来源、已知分析时点和有效期，预览后点“保存报告”。')]),
  pic('09-import.png','图8｜报告导入区。此处为作者编写的操作示例，不是真实AI输出。',155),
  p('软件不会自动读取你的ChatGPT会话。日期不清楚可留空，页面会显示“未知”；不能把证据提取时间写成报告分析时间。JSON中的股票代码必须与当前对象一致。'),
 ]},
 {'title':'内置模板：从合适的问题开始','sub':'先用内置模板跑通流程，再按自己的习惯增删改。','blocks':[
  pic('07-template.png','图9｜“指标组合解读”模板；提示词可以直接编辑。',155),
  table(['模板','适合什么时候用','希望得到什么'],[
   ['快速看懂','第一次研究一只股票','关键依据、反例与下一步'],['短线机会','观察1-3个交易日','确认条件、失效条件与成交限制'],['波段计划','观察5-20个交易日','趋势场景、关键位和复核条件'],['长期公司研究','研究6-24个月','经营、现金流、估值前提与缺失财报'],['持仓复核','已有持仓或计划','原逻辑是否成立，需要补哪些账户信息'],['公告与消息核验','消息驱动或题材线索','事实、原始来源、推断与待核实项'],['指标组合解读','多个技术信号冲突','趋势、动量、量能的阅读顺序'],['反方审查','收到精选或模型结论','最强反证、样本与验证缺口']],[91,159,261]),
  h('管理自己的模板'),
  p('“保存模板”保存当前修改；“新建”建立独立模板；“删除”也可删除内置项；“补回内置”只恢复缺少的内置模板，不覆盖已编辑内容和自定义模板。旧版自定义模板会保留。'),
  p('提示词支持TXT、MD、JSON，限32 KiB；JSON可使用prompt、content或template字符串字段。报告导入限256 KiB。'),
 ]},
 {'title':'阅读报告：先查来源，再看结论','sub':'AI负责解释证据，模型是否通过验证仍以量化状态为准。','blocks':[
  pic('10-report.png','图10｜导入报告与量化对照。报告正文是操作示例，量化栏展示未通过/不可用状态。',285),
  table(['先检查','怎么理解'],[['来源与原文','区分API生成与手动导入。必要时点“查看原文”，不要只看摘要。'],['三个时间','行情/新闻原始时间、证据提取时点、报告分析时点不是一回事；有效期未知也需要复核。'],['量化对照','“AI共识”只表示AI解释的一致性，不等于模型通过验收。未通过或降级状态不能被AI分数覆盖。'],['快照或最新证据','报告快照保留当时上下文；切到最新证据后，旧报告不等于已基于新行情重新分析。']],[95,416]),
  note('报告可复制、导出并在历史中回看。如果页面仍显示“未保存”，点报告区“保存”后再核对历史记录。出现新行情或失效条件时，应重新研究。'),
 ]},
 {'title':'把研究变成可复核的计划','sub':'短线、波段和长期分开记录，不混用目标。','blocks':[
  table(['计划要素','记录什么'],[['研究对象与期限','股票、来源快照，以及短线/波段/长期目标。'],['触发与失效','什么证据出现才继续；什么情况推翻原判断。'],['预算与价格','研究参考价、风险预算及资金限制。软件显示的仓位建议是估算，不保证成交。'],['复核日期','什么时候回来检查；不能无限期沿用一份旧报告。'],['实际成交','成交后按真实价格、数量及费用记账。记录成交不会替你向券商下单。']],[104,407]),
  h('日常操作顺序'),
  steps([('开始研究','检查引擎、行情日期和旧计划，再重扫精选。'),('盘中观察','复核图表、公告来源与触发条件；遇到矛盾先记录，不强行合并结论。'),('收尾复盘','保存报告、更新计划状态；将实际成交与当时依据对应起来。')]),
  pic('13-multi.png','图11｜多图工作区演示，可在各图选择指标；比较时先统一周期。',185),
  p('策略页中的训练、回测和发布状态用于判断模型是否具备使用条件。即使AI赞同，也不能让未通过验证的结果自动变成可用模型。软件本身不承诺盈利。'),
 ]},
 {'title':'常见问题与阅读约定','sub':'遇到问题，先分清数据、配置、规则与操作状态。','blocks':[
  table(['问题','处理方式'],[
   ['精选没有股票？','先检查引擎是否就绪，再重扫。门槛不满足时允许空榜，不需要为了凑5只降低要求。'],
   ['研究分很高，为什么仍有风险？','研究分是规则评价，可能触及100分上限；不是胜率、目标收益或即时可成交判断。'],
   ['看不到历史频率？','样本尚未满足门槛；候选研究和AI复核仍可使用。'],
   ['MA没有画出来？','点击MA或在指标面板点“画到图上”，检查已画标记、周期和历史长度。MA60需要足够K线。'],
   ['AI运行按钮不可用？','检查是否已选择有效A股、平台及可用模型；在工具中的AI平台配置补齐信息。'],
   ['导入后显示日期未知？','只有明确知道原始分析时间或有效期时才填写，不要补猜日期。'],
   ['报告与量化判断不同？','保留分歧，检查数据时点、证据与模型状态。AI结论不能替代量化验收。'],
   ['安装后界面空白？','先关闭并重开，确认使用v2.4.2或更新版本；查看是否出现加载失败提示及重试按钮，仍异常时保留版本和错误信息。'],
  ],[148,363]),
  h('截图与示例说明'),
  p('本书以v2.4.2为准，制作日期2026-09-08。图1是用户电脑上已安装程序的窗口截图；其余图由同版生产前端在隔离演示环境中实际渲染并截图，名称、行情、平台、模型和报告均为操作示例。截图不展示用户账户、密钥或真实AI调用结果。'),
  p('本书说明操作方法，不对画面股票作当前推荐。模板中的时间范围是研究视角；缺少长期财务证据时，AI应明确说明不足。'),
  h('文件使用'),
  p('PDF可直接阅读、检索与打印。Markdown与images文件夹应放在一起，图片路径保持不变；完整压缩包包含两种格式和截图。原始界面细节可在PDF中放大查看。'),
  p('内容依据：当前软件界面、内置模板，以及项目内RESEARCH_WORKFLOW、ADAPTIVE_KING_PICKS和指标实现。后续版本按钮或规则改变时，以应用实际显示为准。'),
 ]},
]

# One content source for both formats.
def md_blocks(blocks):
    result=[]
    for b in blocks:
        t=b['type']
        if t=='p':result.append(b['text'])
        elif t=='h':result.append('### '+b['text'])
        elif t=='note':result.append('> '+b['text'])
        elif t=='image':result.append(f"![{b['caption']}](images/{b['file']})\n\n*{b['caption']}*")
        elif t=='steps':result.append('\n'.join(f"{i}. **{title}**：{text}" for i,(title,text) in enumerate(b['items'],1)))
        elif t=='table':result.append('\n'.join(['| '+' | '.join(b['headers'])+' |','| '+' | '.join(['---']*len(b['headers']))+' |']+['| '+' | '.join(row)+' |' for row in b['rows']]))
        elif t=='cols':result.extend([md_blocks(b['left']),md_blocks(b['right'])])
    return '\n\n'.join(result)

md=f'# {TITLE}\n\n**v2.4.2 · 2026-09-08**\n\n从精选线索到图表证据，再到AI研究与投资计划。\n\n'
md+='![Stock King 图表界面（演示数据）](images/11-chart.png)\n\n## 阅读导航\n\n'
md+='\n'.join(f'- {page["title"]}（PDF第{i+2}页）' for i,page in enumerate(PAGES))+'\n\n'
for page in PAGES:md+='## '+page['title']+'\n\n'+page['sub']+'\n\n'+md_blocks(page['blocks'])+'\n\n'
(OUT/(BASE+'.md')).write_text(md,encoding='utf-8')

pdfmetrics.registerFont(TTFont('CN','C:/Windows/Fonts/msyh.ttc',subfontIndex=0))
pdfmetrics.registerFont(TTFont('CNB','C:/Windows/Fonts/msyhbd.ttc',subfontIndex=0))
pdfmetrics.registerFontFamily('CN',normal='CN',bold='CNB',italic='CN',boldItalic='CNB')
W,H=595.276,841.89
M=42; CW=W-2*M
NAVY=HexColor('#101a2d');INK=HexColor('#23334a');MUTED=HexColor('#64738a');BLUE=HexColor('#3a70d8');PALE=HexColor('#eff4fc');BORDER=HexColor('#dce4f1')
C=canvas.Canvas(str(OUT/(BASE+'.pdf')),pagesize=(W,H),pageCompression=1)
C.setTitle(TITLE);C.setAuthor('Stock King');C.setSubject('应用介绍、精选、K线指标与AI分析使用指南')
records=[]
def text(txt,x,y,width,size=9.5,color=INK,leading=None,bold=False):
    style=ParagraphStyle('manual',fontName='CNB' if bold else 'CN',fontSize=size,leading=leading or size*1.55,textColor=color,wordWrap='CJK',splitLongWords=True,spaceAfter=0)
    para=Paragraph(escape(txt).replace('\n','<br/>'),style)
    _,height=para.wrap(width,H)
    para.drawOn(C,x,y-height)
    return y-height
def image_block(b,x,y,width):
    file=OUT/'images'/b['file'];im=Image.open(file);iw,ih=im.size
    scale=min(width/iw,b['height']/ih);dw,dh=iw*scale,ih*scale
    C.setFillColor(NAVY);C.roundRect(x+(width-dw)/2,y-dh,dw,dh,5,fill=1,stroke=0)
    C.drawImage(ImageReader(str(file)),x+(width-dw)/2,y-dh,width=dw,height=dh,mask='auto')
    return text(b['caption'],x,y-dh-7,width,7.5,MUTED,11)-13
def blocks(items,x,y,width):
    for b in items:
        t=b['type']
        if t=='p':y=text(b['text'],x,y,width)-10
        elif t=='h':y=text(b['text'],x,y-3,width,12,INK,bold=True)-8
        elif t=='note':
            style=ParagraphStyle('note',fontName='CN',fontSize=9,leading=14,textColor=INK,wordWrap='CJK')
            para=Paragraph(escape(b['text']),style);_,ph=para.wrap(width-25,H)
            C.setFillColor(PALE);C.roundRect(x,y-ph-20,width,ph+20,5,stroke=0,fill=1)
            C.setFillColor(BLUE);C.rect(x,y-ph-20,3,ph+20,stroke=0,fill=1)
            para.drawOn(C,x+13,y-ph-10);y-=ph+32
        elif t=='image':y=image_block(b,x,y,width)
        elif t=='cols':
            gap=20;ww=(width-gap)/2
            y=min(blocks(b['left'],x,y,ww),blocks(b['right'],x+ww+gap,y,ww))-4
        elif t=='steps':
            for n,(title,desc) in enumerate(b['items'],1):
                C.setFillColor(PALE);C.circle(x+10,y-10,10,fill=1,stroke=0)
                C.setFillColor(BLUE);C.setFont('CNB',9);C.drawCentredString(x+10,y-13,str(n))
                ty=text(title,x+28,y,width-28,10,INK,bold=True)
                y=text(desc,x+28,ty-3,width-28,9.3)-11
        elif t=='table':
            widths=b.get('widths') or [1]*len(b['headers']);widths=[width*v/sum(widths) for v in widths]
            data=[]
            for r,row in enumerate([b['headers']]+b['rows']):
                st=ParagraphStyle('cell',fontName='CNB' if r==0 else 'CN',fontSize=8.6,leading=12.8,textColor=white if r==0 else INK,wordWrap='CJK')
                data.append([Paragraph(escape(cell),st) for cell in row])
            tb=Table(data,colWidths=widths,hAlign='LEFT')
            tb.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),NAVY),('ROWBACKGROUNDS',(0,1),(-1,-1),[white,PALE]),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7),('LINEBELOW',(0,-1),(-1,-1),.5,BORDER)]))
            _,th=tb.wrap(width,H);tb.drawOn(C,x,y-th);y-=th+13
    return y
def footer(number):
    C.setStrokeColor(BORDER);C.line(M,43,W-M,43)
    text('STOCK KING  /  v2.4.2  /  图文使用说明',M,33,400,7,MUTED)
    C.setFont('CN',8);C.setFillColor(MUTED);C.drawRightString(W-M,23,f'{number:02d} / {len(PAGES)+1:02d}')

# Cover: generous title, real application UI, no fabricated performance chart.
C.setFillColor(NAVY);C.rect(0,0,W,H,fill=1,stroke=0)
C.drawImage(str(ROOT/'desktop/build/appicon.png'),M,H-100,width=44,height=44,mask='auto')
text('STOCK KING',M+60,H-61,430,24,white,bold=True)
text('应用介绍与图文使用说明',M,H-134,CW,26,white,bold=True)
text('精选有依据 · 图表可解读 · AI研究可追溯',M,H-182,CW,12,HexColor('#abc1e7'))
cover=OUT/'images/11-chart.png';iw,ih=Image.open(cover).size;dw=CW;dh=min(360,CW*ih/iw)
C.drawImage(str(cover),M,H-231-dh,width=dw,height=dh,preserveAspectRatio=True,anchor='c',mask='auto')
text('同版生产界面截图，使用隔离演示行情。',M,H-244-dh,CW,7.5,HexColor('#91a8ca'))
for i,label in enumerate(['发现候选','核对图表','AI复核','记录计划']):
    xx=M+i*(CW/4);C.setFillColor(HexColor('#20314f'));C.roundRect(xx,108,CW/4-10,34,5,stroke=0,fill=1)
    text(label,xx+10,132,CW/4-28,10,white)
text('v2.4.2  |  Windows x64  |  2026-09-08',M,74,CW,9,HexColor('#abc1e7'))
text('本书介绍操作流程，不承诺收益。',M,51,CW,8,HexColor('#91a8ca'))
C.bookmarkPage('cover');C.addOutlineEntry('封面','cover',level=0);C.showPage()
for n,page in enumerate(PAGES,2):
    C.bookmarkPage(f'p{n}');C.addOutlineEntry(page['title'],f'p{n}',level=0)
    C.setFillColor(BLUE);C.rect(M,H-48,25,3,fill=1,stroke=0)
    text(f'STOCK KING / 使用指南 / {n-1:02d}',M+35,H-39,CW-35,8,MUTED)
    yy=text(page['title'],M,H-75,CW,23,INK,bold=True)
    yy=text(page['sub'],M,yy-9,CW,10,MUTED)-23
    endy=blocks(page['blocks'],M,yy,CW)
    records.append({'page':n,'title':page['title'],'bottom':round(endy,1)})
    if endy<57:raise RuntimeError(f'Page {n} overflows: {endy:.1f}')
    footer(n);C.showPage()
C.save()
reader=PdfReader(str(OUT/(BASE+'.pdf')))
assert len(reader.pages)==12,len(reader.pages)
alltext='\n'.join(page.extract_text() or '' for page in reader.pages)
for phrase in ['为何入选','反方审查','补回内置','报告分析时点','画到图上']:
    assert phrase in alltext,phrase
assert all((page.extract_text() or '').strip() for page in reader.pages)
(OUT/'build-checks.json').write_text(json.dumps({'pages':records,'pageCount':len(reader.pages),'textChecks':'passed'},ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'pdf':str(OUT/(BASE+'.pdf')),'markdown':str(OUT/(BASE+'.md')),'pages':len(reader.pages),'layout':records},ensure_ascii=False,indent=2))
