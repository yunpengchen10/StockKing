package data

import (
	"encoding/json"
	"testing"
)

func TestSharedMarketNormalizesUnitsAndPreservesUnknownAmount(t *testing.T) {
	for _, source := range []string{"tdx-mac", "eastmoney", "sina", "tencent"} {
		rows := []KLineData{{Low: "9", High: "11", Volume: "100", Amount: "100000"}, {Low: "9", High: "11", Volume: "10000", Amount: "100000"}, {Low: "9", High: "11", Volume: "100", Amount: "700000000"}}
		normalizeSharedVolume(&KLineSourceResult{Data: &rows, Source: source}, "1")
		if rows[0].Volume != "10000" || rows[1].Volume != "10000" || rows[2].Volume != "" || rows[2].Amount != "" {
			t.Fatalf("%s: %+v", source, rows)
		}
	}
	var item SinaKLineItem
	if err := json.Unmarshal([]byte(`{"day":"2026-09-29 09:40:00","close":"10","volume":500}`), &item); err != nil {
		t.Fatal(err)
	}
	api := &SinaKLineApi{}
	rows := api.convertToKLineData([]SinaKLineItem{item}, "1")
	if rows[0].Amount != "" {
		t.Fatal("unknown amount became zero")
	}
}
