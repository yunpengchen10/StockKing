package data

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/bensema/gotdx/proto"
	"github.com/bensema/gotdx/types"
)

type fakeMinuteClient struct {
	active       atomic.Bool
	disconnected atomic.Int32
	read         func(uint8, string, uint16, uint32, uint16) (*proto.MACSymbolBarsReply, error)
}

func (c *fakeMinuteClient) ConnectMAC() error { return nil }
func (c *fakeMinuteClient) Disconnect() error {
	c.disconnected.Add(1)
	return nil
}
func (c *fakeMinuteClient) GetMACSymbolBars(market uint8, code string, period uint16, times uint16, start uint32, count uint16, adjust uint16) (*proto.MACSymbolBarsReply, error) {
	if !c.active.CompareAndSwap(false, true) {
		return nil, fmt.Errorf("concurrent use of one connection")
	}
	defer c.active.Store(false)
	if times != 1 || adjust != types.AdjustNone {
		return nil, fmt.Errorf("unexpected request semantics")
	}
	if c.read != nil {
		return c.read(market, code, period, start, count)
	}
	return minuteReply(market, code, period, start, count), nil
}

func minuteReply(market uint8, code string, period uint16, start uint32, count uint16) *proto.MACSymbolBarsReply {
	price := float64(code[len(code)-1]-'0') + 10
	rows := make([]proto.MACSymbolBar, count)
	for i := range rows {
		rows[i] = proto.MACSymbolBar{DateTime: time.Now().Truncate(time.Minute).Add(-time.Duration(i) * time.Minute),
			Open: price, High: price, Low: price, Close: price, Amount: price * 100, Vol: 100}
	}
	return &proto.MACSymbolBarsReply{Market: uint16(market), Code: code, Period: uint8(period),
		Start: start, Count: count, List: rows}
}

func TestCurrentMinutePoolParallelLeasesRemainBoundedAndKeepIdentities(t *testing.T) {
	entered, release := make(chan string, 8), make(chan struct{})
	var created atomic.Int32
	pool := newMACMinutePool(2, func() pooledMinuteClient {
		created.Add(1)
		return &fakeMinuteClient{read: func(m uint8, code string, p uint16, start uint32, count uint16) (*proto.MACSymbolBarsReply, error) {
			entered <- code
			<-release
			return minuteReply(m, code, p, start, count), nil
		}}
	})
	var tasks sync.WaitGroup
	errors := make(chan error, 8)
	for i := 1; i <= 8; i++ {
		tasks.Add(1)
		go func(i int) {
			defer tasks.Done()
			rows, err := pool.fetch(context.Background(), uint8(types.MarketSH), fmt.Sprintf("60000%d", i), 6)
			if err != nil {
				errors <- err
				return
			}
			if len(rows) != 6 || rows[0].Close != float64(10+i) {
				errors <- fmt.Errorf("wrong security result: %v", rows)
			}
		}(i)
	}
	for i := 0; i < 2; i++ {
		select {
		case <-entered:
		case <-time.After(time.Second):
			close(release)
			t.Fatal("independent connections did not run concurrently")
		}
	}
	if created.Load() != 2 {
		t.Fatalf("created %d clients for two slots", created.Load())
	}
	close(release)
	tasks.Wait()
	close(errors)
	for err := range errors {
		t.Error(err)
	}
	if created.Load() != 2 {
		t.Fatalf("connections were not reused: %d", created.Load())
	}
}

func TestCurrentMinutePoolWaitingLeaseExpiresWithoutCreatingConnection(t *testing.T) {
	entered, release, finished := make(chan struct{}), make(chan struct{}), make(chan struct{})
	var created atomic.Int32
	pool := newMACMinutePool(1, func() pooledMinuteClient {
		created.Add(1)
		return &fakeMinuteClient{read: func(m uint8, code string, p uint16, start uint32, count uint16) (*proto.MACSymbolBarsReply, error) {
			close(entered)
			<-release
			return minuteReply(m, code, p, start, count), nil
		}}
	})
	go func() { defer close(finished); _, _ = pool.fetch(context.Background(), 1, "600001", 6) }()
	<-entered
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	_, err := pool.fetch(ctx, 1, "600002", 6)
	if err != context.DeadlineExceeded || created.Load() != 1 {
		t.Errorf("unbounded waiting lease: %v, clients=%d", err, created.Load())
	}
	close(release)
	<-finished
}

func TestCurrentMinutePoolRejectsWrongReplyAndReconnectsOnlyFailedSlot(t *testing.T) {
	bad := &fakeMinuteClient{read: func(m uint8, code string, p uint16, start uint32, count uint16) (*proto.MACSymbolBarsReply, error) {
		r := minuteReply(m, code, p, start, count)
		r.Code = "600999"
		return r, nil
	}}
	good := &fakeMinuteClient{}
	created := 0
	pool := newMACMinutePool(1, func() pooledMinuteClient {
		created++
		if created == 1 {
			return bad
		}
		return good
	})
	rows, err := pool.fetch(context.Background(), 1, "600001", 6)
	if err != nil || len(rows) != 6 || rows[0].Close != 11 {
		t.Fatalf("retry failed: %v %v", rows, err)
	}
	if created != 2 || bad.disconnected.Load() != 1 || good.disconnected.Load() != 0 {
		t.Fatal("failed connection was not isolated/replaced")
	}
	_, err = pool.fetch(context.Background(), 1, "600002", 6)
	if err != nil || created != 2 {
		t.Fatal("healthy replacement was not reused")
	}
}

func TestCurrentMinutePoolNeverReturnsWrongIdentityOrLateReply(t *testing.T) {
	for _, field := range []string{"code", "market", "period", "start"} {
		t.Run(field, func(t *testing.T) {
			clients := []*fakeMinuteClient{}
			pool := newMACMinutePool(1, func() pooledMinuteClient {
				client := &fakeMinuteClient{read: func(m uint8, code string, p uint16, start uint32, count uint16) (*proto.MACSymbolBarsReply, error) {
					r := minuteReply(m, code, p, start, count)
					switch field {
					case "code":
						r.Code = "600999"
					case "market":
						r.Market++
					case "period":
						r.Period++
					case "start":
						r.Start++
					}
					return r, nil
				}}
				clients = append(clients, client)
				return client
			})
			rows, err := pool.fetch(context.Background(), 1, "600001", 6)
			if err == nil || !strings.Contains(err.Error(), "identity mismatch") || len(rows) != 0 || len(clients) != 2 {
				t.Fatalf("bad reply accepted: %v %v", rows, err)
			}
			for _, c := range clients {
				if c.disconnected.Load() != 1 {
					t.Fatal("bad socket was retained")
				}
			}
		})
	}
	ctx, cancel := context.WithCancel(context.Background())
	late := &fakeMinuteClient{read: func(m uint8, code string, p uint16, start uint32, count uint16) (*proto.MACSymbolBarsReply, error) {
		cancel()
		return minuteReply(m, code, p, start, count), nil
	}}
	pool := newMACMinutePool(1, func() pooledMinuteClient { return late })
	if rows, err := pool.fetch(ctx, 1, "600001", 6); len(rows) != 0 || err != context.Canceled || late.disconnected.Load() != 1 {
		t.Fatalf("late result accepted: %v %v", rows, err)
	}
}

func TestCurrentMinutePoolRoutingLeavesHistoricalAndOtherPeriodsUnchanged(t *testing.T) {
	for _, code := range []string{"600001", "000001.SZ", "300001", "688001.SH"} {
		for _, count := range []int{20, 240} {
			if !useCurrentMinutePool(code, "1", count, types.AdjustNone, "") {
				t.Errorf("current A share not routed: %s count=%d", code, count)
			}
		}
	}
	for _, test := range []struct {
		code, period string
		count        int
		adjust       uint16
		end          string
	}{
		{"600001", "1", 8000, types.AdjustNone, ""},
		{"600001", "1", 241, types.AdjustNone, ""},
		{"600001", "1", 20, types.AdjustNone, "20261021150000"},
		{"600001", "5", 20, types.AdjustNone, ""},
		{"600001", "1", 20, types.AdjustQFQ, ""},
		{"600001", "1", 0, types.AdjustNone, ""},
		{"00700.HK", "1", 20, types.AdjustNone, ""},
		{"920001.BJ", "1", 20, types.AdjustNone, ""},
		{"000300.SH", "1", 20, types.AdjustNone, ""},
		{"00000x.SZ", "1", 20, types.AdjustNone, ""},
	} {
		if useCurrentMinutePool(test.code, test.period, test.count, test.adjust, test.end) {
			t.Errorf("changed other route: %+v", test)
		}
	}
}
