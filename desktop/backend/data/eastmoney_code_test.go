package data

import "testing"

func TestEastMoneyPlainStockCodeUsesMarketPrefix(t *testing.T) {
	api := &EastMoneyKLineApi{}
	for code, want := range map[string]string{
		"001246": "0.001246", "000001": "0.000001", "300001": "0.300001",
		"600000": "1.600000", "688001": "1.688001", "920001": "0.920001",
		"001246.SZ": "0.001246", "1.600000": "1.600000",
		"00e123": "00E123", "00124": "00124", "0012460": "0012460",
	} {
		if got := api.convertStockCode(code); got != want {
			t.Errorf("convertStockCode(%q) = %q, want %q", code, got, want)
		}
	}
}
