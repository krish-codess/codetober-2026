// Package shop is the application whose database gets migrated: an order feed with the defects
// real feeds have, a loader that validates it at the boundary, and writers for two application versions.
package shop

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"math/rand/v2"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

// Order is one validated feed line. Amount stays exactly as the feed spelled it: that mess is what the
// legacy `orders.amount` text column holds, and what migration 02 has to cope with.
type Order struct {
	Ref      string
	Email    string
	Name     *string
	Country  string
	Status   string
	Amount   *string
	Currency string
	PlacedAt time.Time
}

var (
	emailRe   = regexp.MustCompile(`^[^@\s]+@[^@\s]+\.[a-z]{2,}$`)
	alpha2Re  = regexp.MustCompile(`^[A-Z]{2}$`)
	alpha3Re  = regexp.MustCompile(`^[A-Z]{3}$`)
	statusMap = map[string]string{
		"pending": "pending", "paid": "paid", "shipped": "shipped", "cancelled": "cancelled", "refunded": "refunded",
		"complete": "paid", "completed": "paid", "canceled": "cancelled", "refund": "refunded",
	}
	timeLayouts = []string{time.RFC3339, "2006-01-02 15:04:05", "01/02/2006 15:04", "2006-01-02"}
)

// Validate checks one raw feed line. A non-empty reason means the line belongs in quarantine.
func Validate(line []byte, now time.Time) (o Order, reason string) {
	if !utf8.Valid(line) {
		return o, "invalid_utf8"
	}
	var raw struct {
		Ref      *string `json:"order_ref"`
		Email    *string `json:"email"`
		Name     *string `json:"name"`
		Country  string  `json:"country"`
		Status   string  `json:"status"`
		Amount   any     `json:"amount"`
		Currency string  `json:"currency"`
		PlacedAt any     `json:"placed_at"`
	}
	dec := json.NewDecoder(strings.NewReader(string(line)))
	dec.UseNumber()
	if err := dec.Decode(&raw); err != nil {
		return o, "malformed_json"
	}
	if raw.Ref == nil || strings.TrimSpace(*raw.Ref) == "" {
		return o, "missing_order_ref"
	}
	if o.Ref = strings.TrimSpace(*raw.Ref); len(o.Ref) > 64 {
		return o, "order_ref_too_long"
	}
	if raw.Email == nil || strings.TrimSpace(*raw.Email) == "" {
		return o, "missing_email"
	}
	if o.Email = strings.ToLower(strings.TrimSpace(*raw.Email)); !emailRe.MatchString(o.Email) || len(o.Email) > 254 {
		return o, "invalid_email"
	}
	if raw.Name != nil && strings.TrimSpace(*raw.Name) != "" {
		n := strings.TrimSpace(*raw.Name)
		o.Name = &n
	}
	if o.Country = strings.ToUpper(strings.TrimSpace(raw.Country)); !alpha2Re.MatchString(o.Country) {
		return o, "invalid_country"
	}
	var ok bool
	if o.Status, ok = statusMap[strings.ToLower(strings.TrimSpace(raw.Status))]; !ok {
		return o, "unknown_status"
	}
	if o.Currency = strings.ToUpper(strings.TrimSpace(raw.Currency)); o.Currency == "" {
		o.Currency = "USD"
	} else if !alpha3Re.MatchString(o.Currency) {
		return o, "invalid_currency"
	}
	switch a := raw.Amount.(type) {
	case nil:
	case string:
		if len(a) > 32 {
			return o, "amount_too_long"
		}
		o.Amount = &a
	case json.Number:
		s := a.String()
		o.Amount = &s
	default:
		return o, "invalid_amount_type"
	}
	switch p := raw.PlacedAt.(type) {
	case nil:
		return o, "missing_placed_at"
	case json.Number:
		sec, err := p.Int64()
		if err != nil {
			return o, "invalid_placed_at"
		}
		o.PlacedAt = time.Unix(sec, 0).UTC()
	case string:
		for _, layout := range timeLayouts { // layouts without a zone are taken as UTC
			if t, err := time.Parse(layout, strings.TrimSpace(p)); err == nil {
				o.PlacedAt = t.UTC()
				break
			}
		}
		if o.PlacedAt.IsZero() {
			return o, "invalid_placed_at"
		}
	default:
		return o, "invalid_placed_at"
	}
	if o.PlacedAt.After(now.Add(24*time.Hour)) || o.PlacedAt.Year() < 2000 {
		return o, "placed_at_out_of_range"
	}
	return o, ""
}

var (
	// Written without {m,n}: PostgreSQL's regex engine is several times slower on bounded repetition,
	// and public.parse_cents (which must match these exactly) runs once per row in triggers and verification.
	plainRe     = regexp.MustCompile(`^[$]?[0-9]+([.][0-9][0-9]?)?$`)
	thousandsRe = regexp.MustCompile(`^[$]?[0-9][0-9]?[0-9]?(,[0-9][0-9][0-9])+([.][0-9][0-9]?)?$`)
	commaRe     = regexp.MustCompile(`^[0-9]+,[0-9][0-9]?$`)
)

// ParseCents mirrors the SQL function public.parse_cents: the one definition of what a legacy amount means.
// ok is false for text that is not an amount ("N/A", "", "-5.00") or too long to fit in a bigint.
func ParseCents(s string) (cents int64, ok bool) {
	s = strings.Trim(s, " \t\n\r")
	switch {
	case len(s) > 16:
		return 0, false
	case plainRe.MatchString(s):
		s = strings.ReplaceAll(s, "$", "")
	case thousandsRe.MatchString(s):
		s = strings.NewReplacer("$", "", ",", "").Replace(s)
	case commaRe.MatchString(s):
		s = strings.ReplaceAll(s, ",", ".")
	default:
		return 0, false
	}
	whole, frac, _ := strings.Cut(s, ".")
	frac = (frac + "00")[:2]
	w, _ := strconv.ParseInt(whole, 10, 64)
	f, _ := strconv.ParseInt(frac, 10, 64)
	return w*100 + f, true
}

// FormatCents mirrors public.format_cents.
func FormatCents(c int64) string { return fmt.Sprintf("%d.%02d", c/100, c%100) }

// Generate writes n feed lines (plus the duplicates and garbage it injects) as NDJSON. The same seed gives the same bytes.
func Generate(w io.Writer, seed uint64, n int, now time.Time) error {
	r := rand.New(rand.NewPCG(seed, 0x5348_4950))
	bw := bufio.NewWriter(w)
	customers := max(n/8, 10)
	zipf := rand.NewZipf(r, 1.2, 8, uint64(customers-1)) // a few customers place most orders
	countries := []string{"US", "US", "US", "DE", "DE", "GB", "FR", "IN", "IN", "BR", "JP", "NL", "ES", "CA", "AU"}
	currency := map[string]string{"US": "USD", "DE": "EUR", "GB": "GBP", "FR": "EUR", "IN": "INR", "BR": "BRL", "JP": "JPY", "NL": "EUR", "ES": "EUR", "CA": "CAD", "AU": "AUD"}
	names := []string{"Ana Souza", "Liu Wei", "Zoë Müller", "Ravi Kumar", "O'Brien, Pat", "山田 太郎", "Søren Ødegård", "Fatima Al-Sayed", "John Smith", "Chloé Dubois"}
	statuses := []string{"paid", "paid", "paid", "shipped", "shipped", "pending", "cancelled", "refunded"}
	start := now.Add(-400 * 24 * time.Hour)

	var prev string
	for i := 0; i < n; i++ {
		c := int(zipf.Uint64())
		country := countries[c%len(countries)]
		// Orders arrive roughly in time order; 2% arrive late, up to 30 days behind the rest.
		at := start.Add(time.Duration(float64(i) / float64(n) * float64(399*24*time.Hour))).Add(time.Duration(r.IntN(3600)) * time.Second)
		if r.Float64() < 0.02 {
			at = at.Add(-time.Duration(8+r.IntN(22)) * 24 * time.Hour)
		}
		cents := int64(math.Exp(r.NormFloat64()*1.0+3.3) * 100) // log-normal, median about 27.00
		email := fmt.Sprintf("user%d@example.%s", c, strings.ToLower(country))
		m := map[string]any{
			"order_ref": fmt.Sprintf("ORD-%09d", i),
			"email":     email,
			"name":      names[c%len(names)],
			"country":   country,
			"status":    statuses[r.IntN(len(statuses))],
			"currency":  currency[country],
			"amount":    FormatCents(cents),
			"placed_at": at.Format(time.RFC3339),
		}
		// Defects, at the rates documented in docs/DATA_PROFILE.md. Each branch is one way real feeds go wrong.
		switch x := r.Float64(); {
		case x < 0.08:
			m["amount"] = strings.TrimSuffix(FormatCents(cents), "0") // "12.5"
		case x < 0.16:
			m["amount"] = "$" + FormatCents(cents)
		case x < 0.21:
			m["amount"] = strings.Replace(FormatCents(cents), ".", ",", 1) // decimal comma
		case x < 0.24:
			m["amount"] = " " + FormatCents(cents) + " "
		case x < 0.26:
			m["amount"] = fmt.Sprintf("%d,%03d.%02d", cents/100000+1, cents/100%1000, cents%100) // thousands separator
		case x < 0.27:
			m["amount"] = json.Number(FormatCents(cents)) // a JSON number instead of a string
		case x < 0.285:
			m["amount"] = nil
		case x < 0.29:
			m["amount"] = ""
		case x < 0.297:
			m["amount"] = []string{"N/A", "free", "TBD"}[r.IntN(3)]
		case x < 0.30:
			m["amount"] = "-" + FormatCents(cents)
		}
		switch x := r.Float64(); {
		case x < 0.05:
			m["placed_at"] = at.Format("2006-01-02 15:04:05")
		case x < 0.08:
			m["placed_at"] = at.Unix()
		case x < 0.10:
			m["placed_at"] = at.Format("01/02/2006 15:04")
		case x < 0.102:
			m["placed_at"] = "yesterday"
		case x < 0.103:
			delete(m, "placed_at")
		}
		switch x := r.Float64(); {
		case x < 0.03:
			m["status"] = strings.ToUpper(m["status"].(string)) + " "
		case x < 0.05:
			m["status"] = "complete"
		case x < 0.052:
			m["status"] = "on hold"
		}
		switch x := r.Float64(); {
		case x < 0.04:
			m["email"] = "  " + strings.ToUpper(email) // same customer, different spelling
		case x < 0.043:
			m["email"] = strings.Replace(email, "@", " at ", 1)
		case x < 0.045:
			delete(m, "email")
		case x < 0.047:
			delete(m, "order_ref")
		case x < 0.05:
			m["country"] = "Germany"
		case x < 0.07:
			m["country"] = strings.ToLower(country)
		case x < 0.09:
			delete(m, "name")
		}
		line, err := json.Marshal(m)
		if err != nil {
			return err
		}
		switch x := r.Float64(); {
		case x < 0.002:
			line = line[:len(line)/2] // truncated write
		case x < 0.003:
			line = append(line[:len(line)-2], 0xff, 0xfe, '"', '}') // not UTF-8
		}
		bw.Write(line)
		bw.WriteByte('\n')
		if prev != "" && r.Float64() < 0.01 { // redelivery of an earlier line
			bw.WriteString(prev)
			bw.WriteByte('\n')
		}
		prev = string(line)
	}
	return bw.Flush()
}
