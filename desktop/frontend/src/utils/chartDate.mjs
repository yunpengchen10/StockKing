// History paging endpoints require Shanghai calendar time, regardless of OS timezone.
const formatter=new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'})
function parts(ms) { return Number.isFinite(ms) ? Object.fromEntries(formatter.formatToParts(new Date(ms)).map(item=>[item.type,item.value])) : null }
export function formatYmdCompactShanghai(ms) { const p=parts(ms); return p?`${p.year}${p.month}${p.day}`:'' }
export function formatYmdHmsCompactShanghai(ms) { const p=parts(ms); return p?`${p.year}${p.month}${p.day}${p.hour}${p.minute}${p.second}`:'' }
