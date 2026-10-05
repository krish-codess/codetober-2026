package api_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/krish-codess/codetober-2026/SHIP/internal/api"
	"github.com/krish-codess/codetober-2026/SHIP/internal/engine"
	"github.com/krish-codess/codetober-2026/SHIP/internal/testdb"
)

const (
	op   = "operator-secret"
	view = "viewer-secret"
)

type client struct {
	t    *testing.T
	base string
}

// do sends a request and decodes the JSON response into a generic map.
func (c client) do(method, path, token, body string) (int, map[string]any) {
	c.t.Helper()
	req, _ := http.NewRequest(method, c.base+path, strings.NewReader(body))
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		c.t.Fatal(err)
	}
	defer res.Body.Close()
	raw, _ := io.ReadAll(res.Body)
	var out map[string]any
	if len(bytes.TrimSpace(raw)) > 0 {
		if err := json.Unmarshal(raw, &out); err != nil {
			out = map[string]any{"raw": string(raw)}
		}
	}
	if res.Header.Get("X-Request-ID") == "" {
		c.t.Errorf("%s %s: no X-Request-ID on the response", method, path)
	}
	return res.StatusCode, out
}

func file(t *testing.T, rel string) string {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(testdb.Root(), rel))
	if err != nil {
		t.Fatal(err)
	}
	return string(raw)
}

func TestAPI(t *testing.T) {
	d := testdb.New(t)
	eng := d.Engine(t)
	d.Seed(t, eng, 2000)
	handler, _ := api.New(eng, api.Config{OperatorToken: op, ViewerToken: view}, d.Log)
	srv := httptest.NewServer(handler)
	defer srv.Close()
	c := client{t, srv.URL}
	migration := file(t, "migrations/02_amount_to_cents.json")
	desired := file(t, "desired/02_amount_to_cents.json")

	// Every endpoint: who may call it, and what a bad request gets back.
	t.Run("contract", func(t *testing.T) {
		tests := []struct {
			name, method, path, token, body string
			want                            int
		}{
			{"health needs no token", "GET", "/healthz", "", "", 200},
			{"ready needs no token and checks the database", "GET", "/readyz", "", "", 200},
			{"metrics need no token", "GET", "/metrics", "", "", 200},
			{"the API description needs no token", "GET", "/openapi.json", "", "", 200},

			{"list without a token", "GET", "/v1/migrations", "", "", 401},
			{"list with a wrong token", "GET", "/v1/migrations", "nope", "", 401},
			{"list as viewer", "GET", "/v1/migrations", view, "", 200},
			{"list with limit out of range", "GET", "/v1/migrations?limit=0", view, "", 422},
			{"list with a non-numeric cursor", "GET", "/v1/migrations?cursor=abc", view, "", 422},

			{"get without a token", "GET", "/v1/migrations/1", "", "", 401},
			{"get as viewer", "GET", "/v1/migrations/1", view, "", 200},
			{"get a run that does not exist", "GET", "/v1/migrations/999", view, "", 404},
			{"get with a non-numeric id", "GET", "/v1/migrations/abc", view, "", 422},

			{"samples without a token", "GET", "/v1/migrations/1/samples", "", "", 401},
			{"samples as viewer", "GET", "/v1/migrations/1/samples", view, "", 200},
			{"samples of a run that does not exist", "GET", "/v1/migrations/999/samples", view, "", 404},
			{"samples with a malformed cursor", "GET", "/v1/migrations/1/samples?cursor=yesterday", view, "", 422},

			{"plan without a token", "POST", "/v1/plans", "", desired, 401},
			{"plan as viewer", "POST", "/v1/plans", view, desired, 403},
			{"plan with malformed JSON", "POST", "/v1/plans", op, `{"name": `, 400},
			{"plan with a missing field", "POST", "/v1/plans", op, `{"name":"x"}`, 422},
			{"plan that cannot be planned", "POST", "/v1/plans", op, `{"name":"x","compatible_app_versions":["v2"],"tables":{"ghosts":{"columns":[{"name":"id","type":"text"}]}}}`, 422},
			{"plan", "POST", "/v1/plans", op, desired, 200},

			{"submit without a token", "POST", "/v1/migrations", "", migration, 401},
			{"submit as viewer", "POST", "/v1/migrations", view, migration, 403},
			{"submit malformed JSON", "POST", "/v1/migrations", op, `{{`, 400},
			{"submit with no operations", "POST", "/v1/migrations", op, `{"name":"x","compatible_app_versions":["v2"],"operations":[]}`, 422},
			{"submit with a hostile name", "POST", "/v1/migrations", op, `{"name":"x; DROP TABLE orders","compatible_app_versions":["v2"],"operations":[{"sql":{"up":"select 1"}}]}`, 422},
			{"submit an operation pgroll does not know", "POST", "/v1/migrations", op, `{"name":"x","compatible_app_versions":["v2"],"operations":[{"teleport":{}}]}`, 422},
			{"submit against a column that does not exist", "POST", "/v1/migrations", op, `{"name":"x","compatible_app_versions":["v2"],"operations":[{"drop_column":{"table":"orders","column":"ghost","down":"1"}}]}`, 422},
			{"submit with a threshold out of range", "POST", "/v1/migrations", op, `{"name":"x","compatible_app_versions":["v2"],"operations":[{"sql":{"up":"select 1"}}],"settings":{"batch_size":1}}`, 422},

			{"verify without a token", "POST", "/v1/migrations/1/verify", "", "", 401},
			{"verify as viewer", "POST", "/v1/migrations/1/verify", view, "", 403},
			{"verify a run that does not exist", "POST", "/v1/migrations/999/verify", op, "", 404},
			{"verify a completed run", "POST", "/v1/migrations/1/verify", op, "", 409},

			{"complete without a token", "POST", "/v1/migrations/1/complete", "", "", 401},
			{"complete as viewer", "POST", "/v1/migrations/1/complete", view, "", 403},
			{"complete a run that does not exist", "POST", "/v1/migrations/999/complete", op, "", 404},
			{"complete a completed run is a no-op", "POST", "/v1/migrations/1/complete", op, "", 200},

			{"abort without a token", "POST", "/v1/migrations/1/abort", "", `{}`, 401},
			{"abort as viewer", "POST", "/v1/migrations/1/abort", view, `{}`, 403},
			{"abort a run that does not exist", "POST", "/v1/migrations/999/abort", op, `{}`, 404},
			{"abort a completed run", "POST", "/v1/migrations/1/abort", op, `{}`, 409},
			{"abort with an over-long reason", "POST", "/v1/migrations/1/abort", op, `{"reason":"` + strings.Repeat("x", 201) + `"}`, 422},

			{"matrix without a token", "GET", "/v1/compat", "", "", 401},
			{"matrix as viewer", "GET", "/v1/compat", view, "", 200},
			{"deploy gate without a token", "GET", "/v1/compat/check?app_version=v1", "", "", 401},
			{"deploy gate without a version", "GET", "/v1/compat/check", view, "", 422},
			{"deploy gate with a hostile version", "GET", "/v1/compat/check?app_version=v1'--", view, "", 422},
			{"deploy gate", "GET", "/v1/compat/check?app_version=v1", view, "", 200},

			{"duration analytics without a token", "GET", "/v1/analytics/duration-by-size", "", "", 401},
			{"duration analytics", "GET", "/v1/analytics/duration-by-size", view, "", 200},
			{"lock analytics without a token", "GET", "/v1/analytics/lock-waits", "", "", 401},
			{"lock analytics", "GET", "/v1/analytics/lock-waits", view, "", 200},

			{"unknown path", "GET", "/v1/nope", op, "", 404},
		}
		for _, tt := range tests {
			got, body := c.do(tt.method, tt.path, tt.token, tt.body)
			if got != tt.want {
				t.Errorf("%s: %s %s = %d, want %d (%v)", tt.name, tt.method, tt.path, got, tt.want, body)
				continue
			}
			// Errors are problem documents a client can act on, never a bare status or a stack trace.
			if got >= 400 && tt.path != "/v1/nope" {
				if body["status"] != float64(got) || body["title"] == "" || body["detail"] == "" {
					t.Errorf("%s: error body is not a problem document: %v", tt.name, body)
				}
				if s := fmt.Sprint(body); strings.Contains(s, "goroutine") || strings.Contains(s, ".go:") {
					t.Errorf("%s: error leaks internals: %s", tt.name, s)
				}
			}
		}
	})

	// The primary journey, over HTTP: plan, submit, watch, verify, complete.
	t.Run("journey", func(t *testing.T) {
		code, plan := c.do("POST", "/v1/plans", op, desired)
		if code != 200 || len(plan["steps"].([]any)) < 5 {
			t.Fatalf("plan: %d %v", code, plan)
		}
		body, _ := json.Marshal(map[string]any{"name": plan["name"], "compatible_app_versions": plan["compatible_app_versions"], "operations": plan["operations"]})

		code, run := c.do("POST", "/v1/migrations", op, string(body))
		if code != 202 {
			t.Fatalf("submit: %d %v", code, run)
		}
		id := int(run["id"].(float64))
		if run["correlation_id"] == "" {
			t.Error("the run does not carry the request's correlation id")
		}
		if code, again := c.do("POST", "/v1/migrations", op, string(body)); code != 200 || again["id"] != run["id"] {
			t.Errorf("resubmitting: %d, run %v; want 200 and the same run", code, again["id"])
		}
		path := fmt.Sprintf("/v1/migrations/%d", id)

		wait := func(state string) map[string]any {
			t.Helper()
			for deadline := time.Now().Add(time.Minute); time.Now().Before(deadline); time.Sleep(100 * time.Millisecond) {
				if _, r := c.do("GET", path, view, ""); r["state"] == state && (state != engine.Expanded || r["verification"] != nil) {
					return r
				}
			}
			t.Fatalf("run never became %s", state)
			return nil
		}
		expanded := wait(engine.Expanded)
		if expanded["rows_done"] != expanded["rows_total"] || expanded["rows_total"].(float64) < 1900 {
			t.Errorf("progress after expand: %v of %v", expanded["rows_done"], expanded["rows_total"])
		}
		if code, v := c.do("POST", path+"/verify", op, ""); code != 200 || v["ok"] != true {
			t.Errorf("verify: %d %v", code, v)
		}
		if code, s := c.do("GET", path+"/samples?limit=1", view, ""); code != 200 || len(s["items"].([]any)) != 1 || s["next_cursor"] == nil {
			t.Errorf("samples page: %d %v", code, s)
		} else if code, next := c.do("GET", path+"/samples?limit=1&cursor="+strings.ReplaceAll(s["next_cursor"].(string), "+", "%2B"), view, ""); code != 200 ||
			len(next["items"].([]any)) == 1 && next["items"].([]any)[0].(map[string]any)["at"] == s["items"].([]any)[0].(map[string]any)["at"] {
			t.Errorf("the cursor did not advance: %d %v", code, next)
		}
		if code, m := c.do("GET", "/v1/compat", view, ""); code != 200 || len(m["schema_versions"].([]any)) != 2 || len(m["cells"].([]any)) != 4 {
			t.Errorf("matrix: %d %v", code, m)
		}
		if code, chk := c.do("GET", "/v1/compat/check?app_version=v2", view, ""); code != 200 || chk["allowed"] != true {
			t.Errorf("deploy gate for v2: %d %v", code, chk)
		}
		if code, chk := c.do("GET", "/v1/compat/check?app_version=v0", view, ""); code != 200 || chk["allowed"] != false {
			t.Errorf("deploy gate for an unknown version: %d %v", code, chk)
		}

		if code, r := c.do("POST", path+"/complete", op, ""); code != 202 {
			t.Fatalf("complete: %d %v", code, r)
		}
		wait(engine.Completed)

		// Pagination: two runs, one per page, newest first, stable.
		code, first := c.do("GET", "/v1/migrations?limit=1", view, "")
		if code != 200 || first["next_cursor"] == nil || first["items"].([]any)[0].(map[string]any)["id"] != float64(id) {
			t.Fatalf("first page: %d %v", code, first)
		}
		_, second := c.do("GET", "/v1/migrations?limit=1&cursor="+first["next_cursor"].(string), view, "")
		if items := second["items"].([]any); len(items) != 1 || items[0].(map[string]any)["id"] != float64(1) {
			t.Errorf("second page: %v", second)
		}
		_, third := c.do("GET", "/v1/migrations?limit=1&cursor="+second["next_cursor"].(string), view, "")
		if len(third["items"].([]any)) != 0 || third["next_cursor"] != nil {
			t.Errorf("third page should be empty and final: %v", third)
		}

		if code, a := c.do("GET", "/v1/analytics/duration-by-size", view, ""); code != 200 || len(a["points"].([]any)) != 2 {
			t.Errorf("duration by size: %d %v", code, a)
		}
		if code, a := c.do("GET", "/v1/analytics/lock-waits", view, ""); code != 200 || len(a["items"].([]any)) != 2 {
			t.Errorf("lock waits: %d %v", code, a)
		}
	})
}

// The committed API reference is generated from the code; this fails when someone changes one without the other.
func TestOpenAPIDocumentIsCurrent(t *testing.T) {
	_, a := api.New(nil, api.Config{}, nil)
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetIndent("", "  ")
	if err := enc.Encode(a.OpenAPI()); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(testdb.Root(), "docs", "openapi.json")
	if os.Getenv("UPDATE_OPENAPI") != "" {
		if err := os.WriteFile(path, buf.Bytes(), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	committed, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(bytes.ReplaceAll(committed, []byte("\r\n"), []byte("\n")), buf.Bytes()) {
		t.Error("docs/openapi.json is stale; regenerate it with UPDATE_OPENAPI=1 go test ./internal/api -run OpenAPI")
	}
}
