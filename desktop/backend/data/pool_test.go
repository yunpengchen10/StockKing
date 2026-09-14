package data

import (
	"go-stock/backend/db"
	"testing"
)

func TestPool(t *testing.T) {
	db.Init("../../data/stock.db")

	pool := NewBrowserPool(1)
	go pool.FetchPage("https://fund.eastmoney.com/016533.html", "body")
	go pool.FetchPage("https://fund.eastmoney.com/217021.html", "body")
	go pool.FetchPage("https://fund.eastmoney.com/001125.html", "body")

	select {}

}
