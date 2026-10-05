package shop

import (
	"bytes"
	"database/sql"
	"strings"
	"testing"
	"time"
)

var now = time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)

func TestParseCents(t *testing.T) {
	good := map[string]int64{
		"12.50": 1250, "12.5": 1250, "12": 1200, "0": 0, "0.07": 7, "$12.50": 1250, "$3": 300,
		"12,50": 1250, "12,5": 1250, " 12.50 ": 1250, "\t7.00\n": 700,
		"1,234.50": 123450, "1,234": 123400, "$12,345,678.9": 1234567890, "999999999999.99": 99999999999999,
	}
	for in, want := range good {
		if got, ok := ParseCents(in); !ok || got != want {
			t.Errorf("ParseCents(%q) = %d, %v; want %d", in, got, ok, want)
		}
	}
	for _, in := range []string{"", " ", "N/A", "free", "TBD", "-5.00", "12.345", "1,23,456", "12.50 USD", "1e3", "٣", "12..5", ".5", "$", "99999999999999999"} {
		if got, ok := ParseCents(in); ok {
			t.Errorf("ParseCents(%q) = %d, want not an amount", in, got)
		}
	}
	for _, c := range []int64{0, 5, 99, 100, 1250, 123456789} {
		if got, ok := ParseCents(FormatCents(c)); !ok || got != c {
			t.Errorf("round trip of %d through %q gave %d", c, FormatCents(c), got)
		}
	}
}

func TestValidate(t *testing.T) {
	base := `{"order_ref":"A1","email":"A@Example.com ","name":"Ann","country":"de","status":"PAID ","currency":"eur","amount":"12,50","placed_at":"2026-03-01T10:00:00+02:00"}`
	o, reason := Validate([]byte(base), now)
	if reason != "" {
		t.Fatalf("valid line rejected: %s", reason)
	}
	if o.Email != "a@example.com" || o.Country != "DE" || o.Status != "paid" || o.Currency != "EUR" || *o.Amount != "12,50" ||
		!o.PlacedAt.Equal(time.Date(2026, 3, 1, 8, 0, 0, 0, time.UTC)) {
		t.Errorf("not normalised as expected: %+v", o)
	}

	with := func(old, new string) string { return strings.Replace(base, old, new, 1) }
	tests := []struct{ name, line, reason string }{
		{"truncated", base[:40], "malformed_json"},
		{"not json", "order_ref=A1", "malformed_json"},
		{"not utf-8", with("Ann", "An\xff"), "invalid_utf8"},
		{"no order_ref", with(`"order_ref":"A1",`, ""), "missing_order_ref"},
		{"blank order_ref", with(`"A1"`, `"  "`), "missing_order_ref"},
		{"no email", with(`"email":"A@Example.com ",`, ""), "missing_email"},
		{"bad email", with("A@Example.com", "a at example.com"), "invalid_email"},
		{"country name", with(`"de"`, `"Germany"`), "invalid_country"},
		{"unknown status", with("PAID ", "on hold"), "unknown_status"},
		{"bad currency", with(`"eur"`, `"euro"`), "invalid_currency"},
		{"amount is an object", with(`"12,50"`, `{"v":1}`), "invalid_amount_type"},
		{"no placed_at", with(`,"placed_at":"2026-03-01T10:00:00+02:00"`, ""), "missing_placed_at"},
		{"placed_at in words", with("2026-03-01T10:00:00+02:00", "yesterday"), "invalid_placed_at"},
		{"placed_at in the future", with("2026-03-01", "2031-03-01"), "placed_at_out_of_range"},
	}
	for _, tt := range tests {
		if _, got := Validate([]byte(tt.line), now); got != tt.reason {
			t.Errorf("%s: reason %q, want %q", tt.name, got, tt.reason)
		}
	}

	// Things a strict parser would reject but the legacy system accepted, so the loader must too.
	accepted := []struct{ name, line string }{
		{"amount as a number", with(`"12,50"`, `12.5`)},
		{"amount null", with(`"12,50"`, `null`)},
		{"amount garbage, kept verbatim for the legacy column", with(`"12,50"`, `"N/A"`)},
		{"epoch seconds", with(`"2026-03-01T10:00:00+02:00"`, `1772352000`)},
		{"timestamp without zone", with("2026-03-01T10:00:00+02:00", "2026-03-01 08:00:00")},
		{"US date", with("2026-03-01T10:00:00+02:00", "03/01/2026 08:00")},
		{"no name", with(`"name":"Ann",`, "")},
		{"no currency", with(`"currency":"eur",`, "")},
		{"status synonym", with("PAID ", "complete")},
	}
	for _, tt := range accepted {
		if _, reason := Validate([]byte(tt.line), now); reason != "" {
			t.Errorf("%s: rejected as %s", tt.name, reason)
		}
	}
}

func TestGenerateIsDeterministicAndDefective(t *testing.T) {
	var a, b, c bytes.Buffer
	for _, x := range []struct {
		buf  *bytes.Buffer
		seed uint64
	}{{&a, 42}, {&b, 42}, {&c, 43}} {
		if err := Generate(x.buf, x.seed, 20000, now); err != nil {
			t.Fatal(err)
		}
	}
	if !bytes.Equal(a.Bytes(), b.Bytes()) {
		t.Fatal("the same seed produced different feeds")
	}
	if bytes.Equal(a.Bytes(), c.Bytes()) {
		t.Fatal("different seeds produced the same feed")
	}

	reasons := map[string]int{}
	formats := map[string]int{}
	refs := map[string]int{}
	lines := bytes.Split(bytes.TrimSpace(a.Bytes()), []byte("\n"))
	for _, line := range lines {
		o, reason := Validate(line, now)
		reasons[reason]++
		if reason != "" {
			continue
		}
		refs[o.Ref]++
		switch _, ok := ParseCents(deref(o.Amount)); {
		case o.Amount == nil:
			formats["null"]++
		case !ok:
			formats["unreadable"]++
		case strings.ContainsAny(*o.Amount, "$, "):
			formats["messy"]++
		default:
			formats["plain"]++
		}
	}
	for _, want := range []string{"malformed_json", "invalid_utf8", "missing_order_ref", "missing_email", "invalid_email", "invalid_country", "unknown_status", "missing_placed_at", "invalid_placed_at"} {
		if reasons[want] == 0 {
			t.Errorf("the feed has no %s lines; that defect is no longer exercised", want)
		}
	}
	rejected := len(lines) - reasons[""]
	if pct := 100 * float64(rejected) / float64(len(lines)); pct < 0.5 || pct > 5 {
		t.Errorf("%.2f%% of lines rejected; expected a feed that is mostly valid", pct)
	}
	for _, want := range []string{"null", "unreadable", "messy", "plain"} {
		if formats[want] == 0 {
			t.Errorf("no %s amounts in the feed", want)
		}
	}
	dups := 0
	for _, n := range refs {
		dups += n - 1
	}
	if dups == 0 {
		t.Error("the feed has no redelivered lines")
	}
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

func TestAgrees(t *testing.T) {
	str := func(s string) sql.NullString { return sql.NullString{String: s, Valid: true} }
	tests := []struct {
		ref  string
		got  sql.NullString
		v1   bool
		want bool
	}{
		{"w-a-0-1-1250", str("12.50"), true, true},
		{"w-a-0-1-1250", str("$12.50"), true, true},
		{"w-a-0-1-1250", str("12,50"), true, true},
		{"w-a-0-1-1250", str("12.51"), true, false},
		{"w-a-0-1-1250", sql.NullString{}, true, false},
		{"w-a-0-1-1250", str("1250"), false, true},
		{"w-a-0-1-1250", str("1251"), false, false},
		{"w-a-0-1-1250", sql.NullString{}, false, false},
		{"w-a-0-1-x", str("N/A"), true, true},
		{"w-a-0-1-x", sql.NullString{}, false, true},
		{"w-a-0-1-x", str("0"), false, false},
	}
	for _, tt := range tests {
		if got := agrees(tt.ref, tt.got, tt.v1); got != tt.want {
			t.Errorf("agrees(%q, %+v, v1=%v) = %v", tt.ref, tt.got, tt.v1, got)
		}
	}
}
