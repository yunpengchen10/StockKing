export function priceText(value) { return Number.isFinite(value) && value>0 ? String(Number(value.toFixed(4))) : '—' }
export function watchReturn(row) {
 const base=row.SelectionPrice, last=row.Price
 return Number.isFinite(base) && base>0 && Number.isFinite(last) && last>0 && row.LatestQuoteTime ? (last/base-1)*100 : null
}
