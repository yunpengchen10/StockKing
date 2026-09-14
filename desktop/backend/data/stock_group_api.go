package data

import (
	"fmt"
	"go-stock/backend/db"
	"gorm.io/gorm"
	"strings"
	"sync"
	"unicode/utf8"
)

// @Author spark
// @Date 2025/4/3 11:18
// @Desc
// -----------------------------------------------------------------------------------
type Group struct {
	gorm.Model
	Name string `json:"name" gorm:"index"`
	Sort int    `json:"sort"`
}

func (Group) TableName() string {
	return "stock_groups"
}

type GroupStock struct {
	gorm.Model
	StockCode string `json:"stockCode" gorm:"index"`
	GroupId   int    `json:"groupId" gorm:"index"`
	GroupInfo Group  `json:"groupInfo" gorm:"foreignKey:GroupId;references:ID"`
}

func (GroupStock) TableName() string {
	return "group_stock_info"
}

type StockGroupApi struct {
	dao *gorm.DB
}

func NewStockGroupApi(dao *gorm.DB) *StockGroupApi {
	if dao == nil {
		dao = db.Dao
	}
	return &StockGroupApi{dao: dao}
}

var groupMutationMu sync.Mutex

func (receiver StockGroupApi) SaveGroup(id int, name string) error {
	groupMutationMu.Lock()
	defer groupMutationMu.Unlock()
	name = strings.TrimSpace(name)
	if id < 0 || name == "" || name == "全部" || utf8.RuneCountInString(name) > 30 {
		return fmt.Errorf("分组名称须为1至30个字符，不能为全部")
	}
	return receiver.dao.Transaction(func(tx *gorm.DB) error {
		var groups []Group
		if err := tx.Find(&groups).Error; err != nil {
			return err
		}
		maxSort := 0
		for _, g := range groups {
			if int(g.ID) != id && strings.EqualFold(strings.TrimSpace(g.Name), name) {
				return fmt.Errorf("该分组已存在")
			}
			if g.Sort > maxSort {
				maxSort = g.Sort
			}
		}
		if id == 0 {
			return tx.Create(&Group{Name: name, Sort: maxSort + 1}).Error
		}
		var group Group
		if err := tx.First(&group, id).Error; err != nil {
			return fmt.Errorf("分组不存在")
		}
		return tx.Model(&group).Update("name", name).Error
	})
}
func (receiver StockGroupApi) AddGroup(group Group) bool {
	return receiver.SaveGroup(0, group.Name) == nil
}
func (receiver StockGroupApi) GetGroupList() []Group {
	var groups []Group
	receiver.dao.Order("sort ASC").Find(&groups)
	return groups
}
func (receiver StockGroupApi) UpdateGroupSort(id int, newSort int) bool {
	// First, get the current group to check if it exists
	var currentGroup Group
	if err := receiver.dao.First(&currentGroup, id).Error; err != nil {
		return false
	}

	// If the new sort is the same as current, no need to update
	if currentGroup.Sort == newSort {
		return true
	}

	// Get all groups ordered by sort
	var allGroups []Group
	receiver.dao.Order("sort ASC").Find(&allGroups)

	// Adjust sort numbers to make space for the new sort value
	if newSort > currentGroup.Sort {
		// Moving down: decrease sort of groups between old and new position
		receiver.dao.Model(&Group{}).Where("sort > ? AND sort <= ? AND id != ?", currentGroup.Sort, newSort, id).Update("sort", gorm.Expr("sort - ?", 1))
	} else {
		// Moving up: increase sort of groups between new and old position
		receiver.dao.Model(&Group{}).Where("sort >= ? AND sort < ? AND id != ?", newSort, currentGroup.Sort, id).Update("sort", gorm.Expr("sort + ?", 1))
	}

	// Update the target group's sort
	err := receiver.dao.Model(&Group{}).Where("id = ?", id).Update("sort", newSort).Error
	return err == nil
}

// InitializeGroupSort initializes sort order for all groups based on created time
func (receiver StockGroupApi) InitializeGroupSort() bool {
	// Get all groups ordered by created time
	var groups []Group
	err := receiver.dao.Order("created_at ASC").Find(&groups).Error
	if err != nil {
		return false
	}

	// Update each group with new sort value based on their position
	for i, group := range groups {
		newSort := i + 1
		err := receiver.dao.Model(&Group{}).Where("id = ?", group.ID).Update("sort", newSort).Error
		if err != nil {
			return false
		}
	}
	return true
}
func (receiver StockGroupApi) GetGroupStockByGroupId(groupId int) []GroupStock {
	var stockGroup []GroupStock
	receiver.dao.Preload("GroupInfo").Where("group_id = ?", groupId).Find(&stockGroup)
	return stockGroup
}

// GetAllGroupStocks 一次返回全部分组-股票归属记录（预加载 GroupInfo）。
// 用于前端在「全部」标签页表格中渲染每只股票所属的分组，避免 N+1 查询。
func (receiver StockGroupApi) GetAllGroupStocks() []GroupStock {
	var stockGroup []GroupStock
	receiver.dao.Preload("GroupInfo").Find(&stockGroup)
	return stockGroup
}

func (receiver StockGroupApi) AddStockGroup(groupId int, stockCode string) bool {
	groupMutationMu.Lock()
	defer groupMutationMu.Unlock()
	stockCode = normalizeStockCode(stockCode)
	var group Group
	if groupId <= 0 || receiver.dao.First(&group, groupId).Error != nil {
		return false
	}
	err := receiver.dao.Where("group_id = ? and stock_code = ?", groupId, stockCode).FirstOrCreate(&GroupStock{
		GroupId:   groupId,
		StockCode: stockCode,
	}).Updates(&GroupStock{
		GroupId:   groupId,
		StockCode: stockCode,
	}).Error
	return err == nil
}

func (receiver StockGroupApi) RemoveStockGroup(code string, name string, id int) bool {
	groupMutationMu.Lock()
	defer groupMutationMu.Unlock()
	code = normalizeStockCode(code)
	err := receiver.dao.Where("group_id = ? and stock_code = ?", id, code).Delete(&GroupStock{}).Error
	return err == nil
}

func (receiver StockGroupApi) DeleteGroup(id int) error {
	groupMutationMu.Lock()
	defer groupMutationMu.Unlock()
	if id <= 0 {
		return fmt.Errorf("不能删除全部视图")
	}
	return receiver.dao.Transaction(func(tx *gorm.DB) error {
		var group Group
		if err := tx.First(&group, id).Error; err != nil {
			return fmt.Errorf("分组不存在")
		}
		if err := tx.Where("group_id = ?", id).Delete(&GroupStock{}).Error; err != nil {
			return err
		}
		return tx.Delete(&group).Error
	})
}
func (receiver StockGroupApi) RemoveGroup(id int) bool { return receiver.DeleteGroup(id) == nil }
func (receiver StockGroupApi) UpdateGroup(id int, name string) bool {
	if id <= 0 {
		return false
	}
	return receiver.SaveGroup(id, name) == nil
}
