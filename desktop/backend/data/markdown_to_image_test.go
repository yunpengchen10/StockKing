package data

import (
	"strings"
	"testing"
)

func TestIsTableRow(t *testing.T) {
	tests := []struct {
		input string
		want  bool
	}{
		{"| Header 1 | Header 2 |", true},
		{"|---|---|", true},
		{"|:---:|:---:|", true},
		{"| Cell 1 | Cell 2 | Cell 3 |", true},
		{"  | Trimmed | Row |  ", true},
		{"Not a table row", false},
		{"# Heading", false},
		{"", false},
		{"- List item", false},
	}
	for _, tt := range tests {
		got := isTableRow(tt.input)
		if got != tt.want {
			t.Errorf("isTableRow(%q) = %v, want %v", tt.input, got, tt.want)
		}
	}
}

func TestIsTableSeparator(t *testing.T) {
	tests := []struct {
		input string
		want  bool
	}{
		{"|---|---|", true},
		{"|:---:|:---:|", true},
		{"|:---|---:|", true},
		{"| --- | --- |", true},
		{"| Header | Data |", false},
		{"|123|456|", false},
		{"no pipes", false},
	}
	for _, tt := range tests {
		got := isTableSeparator(tt.input)
		if got != tt.want {
			t.Errorf("isTableSeparator(%q) = %v, want %v", tt.input, got, tt.want)
		}
	}
}

func TestParseTableRow(t *testing.T) {
	cells := parseTableRow("| A | B | C |")
	if len(cells) != 3 {
		t.Fatalf("parseTableRow returned %d cells, want 3", len(cells))
	}
	if cells[0] != "A" || cells[1] != "B" || cells[2] != "C" {
		t.Errorf("parseTableRow cells = %v, want [A B C]", cells)
	}
}

func TestSimpleMarkdownToHTML_Table(t *testing.T) {
	input := `| 指标 | 数值 | 变化 |
|------|------|------|
| 市盈率 | 25.3 | +2.1 |
| 市净率 | 3.5 | -0.3 |`

	html := simpleMarkdownToHTML(input)

	if !strings.Contains(html, "<table>") {
		t.Error("expected <table> in output")
	}
	if !strings.Contains(html, "</table>") {
		t.Error("expected </table> in output")
	}
	if !strings.Contains(html, "<thead>") {
		t.Error("expected <thead> in output")
	}
	if !strings.Contains(html, "<tbody>") {
		t.Error("expected <tbody> in output")
	}
	if !strings.Contains(html, "<th>指标</th>") {
		t.Error("expected <th>指标</th> in output")
	}
	if !strings.Contains(html, "<td>25.3</td>") {
		t.Error("expected <td>25.3</td> in output")
	}
}

func TestSimpleMarkdownToHTML_TableWithInlineFormat(t *testing.T) {
	input := `| 项目 | 状态 |
|------|------|
| **重点** | ` + "`完成`" + ` |`

	html := simpleMarkdownToHTML(input)

	if !strings.Contains(html, "<strong>重点</strong>") {
		t.Error("expected bold formatting in table cell")
	}
	if !strings.Contains(html, "<code>") || !strings.Contains(html, "完成") {
		t.Error("expected code formatting with '完成' in table cell")
	}
}

func TestSimpleMarkdownToHTML_MixedContent(t *testing.T) {
	input := `# 股票分析

以下是关键指标：

| 指标 | 数值 |
|------|------|
| PE | 15 |

以上数据仅供参考。`

	html := simpleMarkdownToHTML(input)

	if !strings.Contains(html, "<h1>股票分析</h1>") {
		t.Error("expected h1 heading")
	}
	if !strings.Contains(html, "<table>") {
		t.Error("expected table")
	}
	if !strings.Contains(html, "<th>指标</th>") {
		t.Error("expected table header")
	}
	if !strings.Contains(html, "<td>15</td>") {
		t.Error("expected table data cell")
	}
	if !strings.Contains(html, "<p>以上数据仅供参考。</p>") {
		t.Error("expected paragraph after table")
	}
}

func TestSimpleMarkdownToHTML_SinglePipeLine(t *testing.T) {
	input := `This has a | pipe character`

	html := simpleMarkdownToHTML(input)

	if strings.Contains(html, "<table>") {
		t.Error("single pipe line should not be parsed as table")
	}
	if !strings.Contains(html, "<p>") {
		t.Error("single pipe line should be wrapped in <p>")
	}
}

func TestSimpleMarkdownToHTML_TableNoSeparator(t *testing.T) {
	input := `| A | B |
| C | D |`

	html := simpleMarkdownToHTML(input)

	if !strings.Contains(html, "<table>") {
		t.Error("expected table even without separator row")
	}
	if !strings.Contains(html, "<th>A</th>") {
		t.Error("expected first row as header")
	}
	if !strings.Contains(html, "<td>C</td>") {
		t.Error("expected second row as data")
	}
}

func TestInlineFormat_NoSemanticColor(t *testing.T) {
	result := inlineFormat("涨幅 +1.5% 买入 量产")
	if strings.Contains(result, "class=\"up\"") {
		t.Error("inlineFormat should NOT add up/down color to body text")
	}
	if strings.Contains(result, "class=\"tag") {
		t.Error("inlineFormat should NOT add tag color to body text")
	}
}

func TestCellFormat_SemanticColor(t *testing.T) {
	tests := []struct {
		input    string
		contains string
		label    string
	}{
		{"+1.5%", `<span class="up">+1.5%</span>`, "+1.5% should be red(up)"},
		{"-2.3%", `<span class="down">-2.3%</span>`, "-2.3% should be green(down)"},
		{"涨0.9%", `<span class="up">+0.9%</span>`, "涨0.9% should become +0.9% and red"},
		{"涨1%", `<span class="up">+1%</span>`, "涨1% should become +1% and red"},
		{"涨10.5%", `<span class="up">+10.5%</span>`, "涨10.5% should become +10.5% and red"},
		{"跌0.5%", `<span class="down">-0.5%</span>`, "跌0.5% should become -0.5% and green"},
		{"+3.2pp", `<span class="up">+3.2pp</span>`, "+3.2pp should be red(up)"},
		{"-1.8pp", `<span class="down">-1.8pp</span>`, "-1.8pp should be green(down)"},
		{"买入", `<span class="tag tag-red">买入</span>`, "买入 should be red tag"},
		{"卖出", `<span class="tag tag-green">卖出</span>`, "卖出 should be green tag"},
	}
	for _, tt := range tests {
		result := cellFormat(tt.input)
		if !strings.Contains(result, tt.contains) {
			t.Errorf("%s: cellFormat(%q) = %q, want to contain %q", tt.label, tt.input, result, tt.contains)
		}
	}
}
