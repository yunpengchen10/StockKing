package marketminute

import (
	"fmt"
	"testing"
	"time"

	"github.com/bensema/gotdx/proto"
)

func fixture(n int) []proto.MACSymbolBar {
	latest := time.Date(2026, 10, 9, 11, 30, 0, 0, time.Local)
	rows := make([]proto.MACSymbolBar, n)
	for i := range rows {
		rows[i].DateTime = latest.Add(-time.Duration(i) * time.Minute)
	}
	return rows
}

func TestShortMACPagesDoNotStopHistoryOrSkipBoundaryMinutes(t *testing.T) {
	all := fixture(9000)
	calls := 0
	rows, err := Collect(func(start uint32, size uint16) ([]proto.MACSymbolBar, error) {
		calls++
		end := int(start) + int(size) - 1 // Same 699-for-700 behavior observed on the live source.
		if end > len(all) {
			end = len(all)
		}
		return all[int(start):end], nil
	}, 8000, "")
	if err != nil || len(rows) != 8000 || calls < 12 {
		t.Fatalf("count=%d calls=%d error=%v", len(rows), calls, err)
	}
	for i := 1; i < len(rows); i++ {
		if rows[i].DateTime.Sub(rows[i-1].DateTime) != time.Minute {
			t.Fatalf("gap at %d", i)
		}
	}
}

func TestHistoricalCutoffPagesPastNewerRowsAndReturnsOrderedUniqueBars(t *testing.T) {
	all := fixture(4000)
	end := all[1500].DateTime.Format("20060102150405")
	rows, err := Collect(func(start uint32, size uint16) ([]proto.MACSymbolBar, error) {
		last := int(start) + int(size) - 1
		if last > len(all) {
			last = len(all)
		}
		return all[int(start):last], nil
	}, 800, end)
	if err != nil || len(rows) != 800 {
		t.Fatalf("count=%d error=%v", len(rows), err)
	}
	if !rows[len(rows)-1].DateTime.Equal(all[1500].DateTime) || !rows[0].DateTime.Equal(all[2299].DateTime) {
		t.Fatal("wrong historical window")
	}
}

func TestIgnoredOffsetTerminatesAndErrorsRemainVisible(t *testing.T) {
	calls := 0
	rows, err := Collect(func(uint32, uint16) ([]proto.MACSymbolBar, error) { calls++; return fixture(10), nil }, 100, "")
	if err != nil || calls != 2 || len(rows) != 10 {
		t.Fatalf("calls=%d count=%d error=%v", calls, len(rows), err)
	}
	_, err = Collect(func(uint32, uint16) ([]proto.MACSymbolBar, error) { return nil, fmt.Errorf("network timeout") }, 100, "")
	if err == nil {
		t.Fatal("fetch error hidden")
	}
}

func TestCollectionCutoffCannotMaturePageOneWhileDownloadingLaterPages(t *testing.T) {
	started := time.Date(2026, 10, 9, 11, 26, 58, 0, time.Local)
	rows := fixture(10)
	cutoff := minuteCutoff("", started)
	if cutoff != "20261009112658" || minuteCutoff("20261009113000", started) != cutoff {
		t.Fatal("latest request must retain its start cutoff")
	}
	if minuteCutoff("20260930", started) != "20260930235959" {
		t.Fatal("historical cutoff changed")
	}
	result, err := Collect(func(uint32, uint16) ([]proto.MACSymbolBar, error) { return rows, nil }, 10, cutoff)
	if err != nil {
		t.Fatal(err)
	}
	for _, row := range result {
		if row.DateTime.After(started) {
			t.Fatal("partial page-one minute was accepted")
		}
	}
}

func TestPagedMACBarsPreserveTurnoverCalculation(t *testing.T) {
	rows := fixture(1)
	rows[0].Vol = 123456
	rows[0].FloatShares = 1000
	result, err := Collect(func(uint32, uint16) ([]proto.MACSymbolBar, error) { return rows, nil }, 1, "")
	if err != nil || result[0].Turnover != 1.23 {
		t.Fatalf("turnover lost: %v %v", result, err)
	}
}
