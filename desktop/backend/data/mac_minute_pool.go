package data

import (
	"context"
	"fmt"
	"go-stock/backend/marketminute"
	"time"

	gotdx "github.com/bensema/gotdx"
	"github.com/bensema/gotdx/proto"
	"github.com/bensema/gotdx/types"
)

const currentMinutePoolSize = 8
const currentMinuteRequestTimeout = 5 * time.Second

type pooledMinuteClient interface {
	marketminute.BarClient
	Disconnect() error
}

type minuteClientSlot struct {
	client pooledMinuteClient
}

// A lease is the per-connection lock: one request owns a socket through every
// page, identity check and reconnect. The fixed slots bound live connections;
// waiting requests cannot create clients or block historical minute pagination.
type macMinutePool struct {
	slots  chan *minuteClientSlot
	create func() pooledMinuteClient
}

func newMACMinutePool(size int, create func() pooledMinuteClient) *macMinutePool {
	pool := &macMinutePool{slots: make(chan *minuteClientSlot, size), create: create}
	for i := 0; i < size; i++ {
		pool.slots <- &minuteClientSlot{}
	}
	return pool
}

func (pool *macMinutePool) fetch(ctx context.Context, market uint8, code string, count int) ([]proto.MACSymbolBar, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	var slot *minuteClientSlot
	select {
	case slot = <-pool.slots:
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	defer func() { pool.slots <- slot }()
	var last error
	for attempt := 0; attempt < 2; attempt++ {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if slot.client == nil {
			slot.client = pool.create()
		}
		if slot.client == nil {
			return nil, fmt.Errorf("minute connection unavailable")
		}
		rows, err := marketminute.FetchContext(ctx, slot.client, market, code,
			types.KLINE_TYPE_1MIN, count, types.AdjustNone, "")
		if ctx.Err() != nil {
			err = ctx.Err()
		}
		if err == nil {
			return rows, nil
		}
		// A transport or identity failure invalidates only this connection.
		// No mismatched page can enter the shared quote cache.
		last = err
		_ = slot.client.Disconnect()
		slot.client = nil
	}
	return nil, last
}

func useCurrentMinutePool(stockCode, period string, count int, adjust uint16, end string) bool {
	// The candidate path asks for a whole current session (240 minutes), while
	// sector peers ask for 20. Both are live requests; dated/backfill pages keep
	// their existing independent connection and cannot queue behind this pool.
	if period != "1" || count < 1 || count > 240 || adjust != types.AdjustNone || end != "" {
		return false
	}
	market, code := tdxMarketFromStockCode(stockCode)
	if len(code) != 6 || (market != uint8(types.MarketSH) && market != uint8(types.MarketSZ)) {
		return false
	}
	for _, digit := range code {
		if digit < '0' || digit > '9' {
			return false
		}
	}
	return (market == uint8(types.MarketSH) && (code[:2] == "60" || code[:2] == "68")) ||
		(market == uint8(types.MarketSZ) && (code[:2] == "00" || code[:2] == "30"))
}

func (t *TdxKLineApi) currentMinuteBars(market uint8, code string, count int) ([]proto.MACSymbolBar, error) {
	t.minutePoolOnce.Do(func() {
		t.minutePool = newMACMinutePool(currentMinutePoolSize, func() pooledMinuteClient {
			timeout := GetSettingConfig().CrawlTimeOut
			if timeout <= 0 || timeout > 5 {
				timeout = 5
			}
			return gotdx.NewMAC(gotdx.WithAutoSelectFastest(true), gotdx.WithTimeoutSec(int(timeout)))
		})
	})
	ctx, cancel := context.WithTimeout(context.Background(), currentMinuteRequestTimeout)
	defer cancel()
	return t.minutePool.fetch(ctx, market, code, count)
}
