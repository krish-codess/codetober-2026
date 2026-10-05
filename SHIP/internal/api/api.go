// Package api is the HTTP contract of the controller. The OpenAPI document is generated
// from the types and operations registered here, so it cannot drift from the code.
package api

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"log/slog"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/danielgtaylor/huma/v2"
	"github.com/danielgtaylor/huma/v2/adapters/humago"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"

	"github.com/krish-codess/codetober-2026/SHIP/internal/engine"
	"github.com/krish-codess/codetober-2026/SHIP/internal/plan"
)

var (
	httpRequests = promauto.NewCounterVec(prometheus.CounterOpts{Name: "shipd_http_requests_total", Help: "HTTP requests by route and status."}, []string{"route", "code"})
	httpDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{Name: "shipd_http_request_duration_seconds", Help: "HTTP request duration by route.", Buckets: prometheus.DefBuckets}, []string{"route"})
)

type ctxKey struct{}

// CorrelationID returns the request's correlation id; it is stored on any run the request creates.
func CorrelationID(ctx context.Context) string {
	id, _ := ctx.Value(ctxKey{}).(string)
	return id
}

type Config struct {
	OperatorToken string // may change things
	ViewerToken   string // may only read
}

const (
	viewer   = "viewer"
	operator = "operator"
)

func secured(role string) []map[string][]string { return []map[string][]string{{"bearer": {role}}} }

type runOut struct {
	Status int
	Body   engine.Run
}

type page[T any] struct {
	Items      []T    `json:"items"`
	NextCursor string `json:"next_cursor,omitempty" doc:"Pass as cursor to get the next page; absent on the last page"`
}

type idIn struct {
	ID int64 `path:"id" minimum:"1" doc:"Run id"`
}

// New builds the handler. eng may be nil when only the OpenAPI document is wanted.
func New(eng *engine.Engine, cfg Config, log *slog.Logger) (http.Handler, huma.API) {
	mux := http.NewServeMux()
	hc := huma.DefaultConfig("shipd: zero-downtime schema migrations", "1.0.0")
	hc.Info.Description = "Drives pgroll migrations through expand, verify and contract under live load, and reverts them when locks or errors exceed their thresholds. " +
		"Reads need the viewer or operator token, everything else the operator token, as `Authorization: Bearer <token>`. Errors are RFC 9457 problem documents."
	hc.Components.SecuritySchemes = map[string]*huma.SecurityScheme{"bearer": {Type: "http", Scheme: "bearer"}}
	api := humago.New(mux, hc)

	api.UseMiddleware(func(ctx huma.Context, next func(huma.Context)) {
		sec := ctx.Operation().Security
		if len(sec) == 0 {
			next(ctx)
			return
		}
		token, ok := strings.CutPrefix(ctx.Header("Authorization"), "Bearer ")
		is := func(want string) bool {
			return want != "" && subtle.ConstantTimeCompare([]byte(token), []byte(want)) == 1
		}
		switch {
		case !ok || !(is(cfg.OperatorToken) || is(cfg.ViewerToken)):
			ctx.SetHeader("WWW-Authenticate", "Bearer")
			_ = huma.WriteErr(api, ctx, http.StatusUnauthorized, "missing or invalid bearer token")
		case sec[0]["bearer"][0] == operator && !is(cfg.OperatorToken):
			_ = huma.WriteErr(api, ctx, http.StatusForbidden, "this token may only read; the operation needs the operator token")
		default:
			next(ctx)
		}
	})

	fail := func(err error) error {
		var e *engine.Error
		var pe *plan.Error
		switch {
		case errors.As(err, &pe):
			return huma.Error422UnprocessableEntity("the desired schema cannot be planned", toErrs(pe.Problems)...)
		case !errors.As(err, &e):
			log.Error("internal error", "err", err)
			return huma.Error500InternalServerError("internal error; see the controller log")
		case e.Kind == "not_found":
			return huma.Error404NotFound(e.Msg)
		case e.Kind == "invalid":
			return huma.Error422UnprocessableEntity(e.Msg, toErrs(e.Details)...)
		default:
			return huma.Error409Conflict(e.Msg, toErrs(e.Details)...)
		}
	}

	// ---- health ----
	huma.Register(api, huma.Operation{OperationID: "healthz", Method: http.MethodGet, Path: "/healthz", Tags: []string{"health"},
		Summary: "Liveness: the process is serving"},
		func(ctx context.Context, _ *struct{}) (*struct {
			Body struct {
				Status string `json:"status"`
			}
		}, error) {
			out := &struct {
				Body struct {
					Status string `json:"status"`
				}
			}{}
			out.Body.Status = "ok"
			return out, nil
		})
	huma.Register(api, huma.Operation{OperationID: "readyz", Method: http.MethodGet, Path: "/readyz", Tags: []string{"health"},
		Summary: "Readiness: database reachable, pgroll initialised, leader lock held", Errors: []int{503}},
		func(ctx context.Context, _ *struct{}) (*struct {
			Body struct {
				Status string `json:"status"`
			}
		}, error) {
			ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
			defer cancel()
			if err := eng.Ready(ctx); err != nil {
				return nil, huma.Error503ServiceUnavailable("not ready: " + err.Error())
			}
			out := &struct {
				Body struct {
					Status string `json:"status"`
				}
			}{}
			out.Body.Status = "ready"
			return out, nil
		})

	// ---- plans ----
	huma.Register(api, huma.Operation{OperationID: "plan", Method: http.MethodPost, Path: "/v1/plans", Tags: []string{"migrations"},
		Summary:     "Plan a migration from a schema diff",
		Description: "Compares the desired schema with the live one and returns the expand-contract plan: pgroll operations, the steps of each phase with the locks they take, and a backfill estimate. Changes nothing.",
		Security:    secured(operator), Errors: []int{401, 403, 422}},
		func(ctx context.Context, in *struct{ Body plan.Desired }) (*struct{ Body plan.Plan }, error) {
			current, err := plan.Load(ctx, eng.DB())
			if err != nil {
				return nil, fail(err)
			}
			hist, err := eng.DurationBySize(ctx)
			if err != nil {
				return nil, fail(err)
			}
			p, err := plan.Diff(current, in.Body, hist.MedianRowsPerSec)
			if err != nil {
				return nil, fail(err)
			}
			return &struct{ Body plan.Plan }{p}, nil
		})

	// ---- migrations ----
	huma.Register(api, huma.Operation{OperationID: "submit-migration", Method: http.MethodPost, Path: "/v1/migrations", Tags: []string{"migrations"},
		Summary:     "Submit a migration and start expanding it",
		Description: "Returns 202 for a new run. Safe to retry: submitting the same name and operations again returns the existing run with 200. A different migration under the same name, or any submission while another migration is in flight, is a 409.",
		Security:    secured(operator), DefaultStatus: 202, Errors: []int{401, 403, 409, 422}},
		func(ctx context.Context, in *struct{ Body engine.Submit }) (*runOut, error) {
			run, created, err := eng.Submit(ctx, in.Body, CorrelationID(ctx))
			if err != nil {
				return nil, fail(err)
			}
			if !created {
				return &runOut{Status: http.StatusOK, Body: run}, nil
			}
			return &runOut{Status: http.StatusAccepted, Body: run}, nil
		})

	huma.Register(api, huma.Operation{OperationID: "list-migrations", Method: http.MethodGet, Path: "/v1/migrations", Tags: []string{"migrations"},
		Summary: "List migration runs, newest first", Security: secured(viewer), Errors: []int{401, 422}},
		func(ctx context.Context, in *struct {
			Cursor int64 `query:"cursor" minimum:"0" doc:"next_cursor of the previous page"`
			Limit  int   `query:"limit" minimum:"1" maximum:"200" default:"50"`
		}) (*struct{ Body page[engine.Run] }, error) {
			runs, err := eng.List(ctx, in.Cursor, in.Limit)
			if err != nil {
				return nil, fail(err)
			}
			out := &struct{ Body page[engine.Run] }{}
			out.Body.Items = runs
			if len(runs) == in.Limit {
				out.Body.NextCursor = strconv.FormatInt(runs[len(runs)-1].ID, 10)
			}
			return out, nil
		})

	huma.Register(api, huma.Operation{OperationID: "get-migration", Method: http.MethodGet, Path: "/v1/migrations/{id}", Tags: []string{"migrations"},
		Summary: "Get a run with its progress", Security: secured(viewer), Errors: []int{401, 404, 422}},
		func(ctx context.Context, in *idIn) (*struct{ Body engine.Run }, error) {
			run, err := eng.Get(ctx, in.ID)
			if err != nil {
				return nil, fail(err)
			}
			return &struct{ Body engine.Run }{run}, nil
		})

	huma.Register(api, huma.Operation{OperationID: "list-samples", Method: http.MethodGet, Path: "/v1/migrations/{id}/samples", Tags: []string{"migrations"},
		Summary: "Lock and error samples the guard took during a run, oldest first", Security: secured(viewer), Errors: []int{401, 404, 422}},
		func(ctx context.Context, in *struct {
			ID     int64     `path:"id" minimum:"1" doc:"Run id"`
			Cursor time.Time `query:"cursor" doc:"next_cursor of the previous page (the at of its last sample)"`
			Limit  int       `query:"limit" minimum:"1" maximum:"2000" default:"500"`
		}) (*struct{ Body page[engine.Sample] }, error) {
			if _, err := eng.Get(ctx, in.ID); err != nil {
				return nil, fail(err)
			}
			samples, err := eng.Samples(ctx, in.ID, in.Cursor, in.Limit)
			if err != nil {
				return nil, fail(err)
			}
			out := &struct{ Body page[engine.Sample] }{}
			out.Body.Items = samples
			if len(samples) == in.Limit {
				out.Body.NextCursor = samples[len(samples)-1].At.Format(time.RFC3339Nano)
			}
			return out, nil
		})

	huma.Register(api, huma.Operation{OperationID: "verify-migration", Method: http.MethodPost, Path: "/v1/migrations/{id}/verify", Tags: []string{"migrations"},
		Summary:     "Compare old and new representations across every row",
		Description: "Only possible while the run is expanded. Read-only apart from copying lossy rows to quarantine, so it can be repeated freely.",
		Security:    secured(operator), Errors: []int{401, 403, 404, 409, 422}},
		func(ctx context.Context, in *idIn) (*struct{ Body engine.Verification }, error) {
			v, err := eng.Verify(ctx, in.ID)
			if err != nil {
				return nil, fail(err)
			}
			return &struct{ Body engine.Verification }{v}, nil
		})

	huma.Register(api, huma.Operation{OperationID: "complete-migration", Method: http.MethodPost, Path: "/v1/migrations/{id}/complete", Tags: []string{"migrations"},
		Summary:     "Contract: drop the old representation",
		Description: "Refused with 409 unless verification passes and no connected application version depends on the old schema version. Returns 202 while contracting; poll the run. Safe to retry.",
		Security:    secured(operator), DefaultStatus: 202, Errors: []int{401, 403, 404, 409, 422}},
		func(ctx context.Context, in *idIn) (*runOut, error) {
			run, err := eng.Complete(ctx, in.ID)
			if err != nil {
				return nil, fail(err)
			}
			return &runOut{Status: statusFor(run), Body: run}, nil
		})

	huma.Register(api, huma.Operation{OperationID: "abort-migration", Method: http.MethodPost, Path: "/v1/migrations/{id}/abort", Tags: []string{"migrations"},
		Summary:     "Abort and revert a migration that has not been contracted",
		Description: "Refused with 409 if a connected application version can only run on the new schema version. Returns 202 while reverting; poll the run. Safe to retry.",
		Security:    secured(operator), DefaultStatus: 202, Errors: []int{401, 403, 404, 409, 422}},
		func(ctx context.Context, in *struct {
			ID   int64 `path:"id" minimum:"1" doc:"Run id"`
			Body struct {
				Reason string `json:"reason,omitempty" maxLength:"200" doc:"Recorded on the run"`
			}
		}) (*runOut, error) {
			run, err := eng.Abort(ctx, in.ID, in.Body.Reason)
			if err != nil {
				return nil, fail(err)
			}
			return &runOut{Status: statusFor(run), Body: run}, nil
		})

	// ---- compatibility ----
	huma.Register(api, huma.Operation{OperationID: "get-compat", Method: http.MethodGet, Path: "/v1/compat", Tags: []string{"compatibility"},
		Summary: "Compatibility matrix: application versions against schema versions, with live connections", Security: secured(viewer), Errors: []int{401}},
		func(ctx context.Context, _ *struct{}) (*struct{ Body engine.Matrix }, error) {
			m, err := eng.Compat(ctx)
			if err != nil {
				return nil, fail(err)
			}
			return &struct{ Body engine.Matrix }{m}, nil
		})

	huma.Register(api, huma.Operation{OperationID: "check-compat", Method: http.MethodGet, Path: "/v1/compat/check", Tags: []string{"compatibility"},
		Summary: "Deploy gate: may this application version be rolled out now?", Security: secured(viewer), Errors: []int{401, 422}},
		func(ctx context.Context, in *struct {
			AppVersion string `query:"app_version" required:"true" pattern:"^[A-Za-z0-9._-]{1,20}$"`
		}) (*struct{ Body engine.CompatCheck }, error) {
			c, err := eng.Check(ctx, in.AppVersion)
			if err != nil {
				return nil, fail(err)
			}
			return &struct{ Body engine.CompatCheck }{c}, nil
		})

	// ---- analytics ----
	huma.Register(api, huma.Operation{OperationID: "duration-by-size", Method: http.MethodGet, Path: "/v1/analytics/duration-by-size", Tags: []string{"analytics"},
		Summary: "Migration duration against table size", Security: secured(viewer), Errors: []int{401}},
		func(ctx context.Context, _ *struct{}) (*struct{ Body engine.DurationBySize }, error) {
			d, err := eng.DurationBySize(ctx)
			if err != nil {
				return nil, fail(err)
			}
			return &struct{ Body engine.DurationBySize }{d}, nil
		})

	huma.Register(api, huma.Operation{OperationID: "lock-waits", Method: http.MethodGet, Path: "/v1/analytics/lock-waits", Tags: []string{"analytics"},
		Summary: "Lock wait time during each migration", Security: secured(viewer), Errors: []int{401}},
		func(ctx context.Context, _ *struct{}) (*struct {
			Body struct {
				Items []engine.LockWait `json:"items"`
			}
		}, error) {
			w, err := eng.LockWaits(ctx)
			if err != nil {
				return nil, fail(err)
			}
			out := &struct {
				Body struct {
					Items []engine.LockWait `json:"items"`
				}
			}{}
			out.Body.Items = w
			return out, nil
		})

	mux.Handle("GET /metrics", promhttp.Handler())
	return observe(mux, log), api
}

func statusFor(run engine.Run) int {
	if run.State == engine.Completed || run.State == engine.Reverted {
		return http.StatusOK
	}
	return http.StatusAccepted
}

func toErrs(details []string) []error {
	out := make([]error, len(details))
	for i, d := range details {
		out[i] = errors.New(d)
	}
	return out
}

type recorder struct {
	http.ResponseWriter
	code int
}

func (r *recorder) WriteHeader(code int) { r.code = code; r.ResponseWriter.WriteHeader(code) }

var safeID = regexp.MustCompile(`^[A-Za-z0-9._-]{1,64}$`)

// observe gives every request a correlation id, a structured log line and metrics.
func observe(next http.Handler, log *slog.Logger) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := r.Header.Get("X-Request-ID")
		if !safeID.MatchString(id) {
			b := make([]byte, 8)
			_, _ = rand.Read(b)
			id = hex.EncodeToString(b)
		}
		w.Header().Set("X-Request-ID", id)
		r.Body = http.MaxBytesReader(w, r.Body, 1<<20)
		rec := &recorder{ResponseWriter: w, code: 200}
		start := time.Now()
		r = r.WithContext(context.WithValue(r.Context(), ctxKey{}, id))
		next.ServeHTTP(rec, r)
		route := r.Pattern // set by the mux; empty for unmatched paths, which keeps label cardinality bounded
		if route == "" {
			route = "unmatched"
		}
		httpRequests.WithLabelValues(route, strconv.Itoa(rec.code)).Inc()
		httpDuration.WithLabelValues(route).Observe(time.Since(start).Seconds())
		if r.URL.Path != "/healthz" && r.URL.Path != "/readyz" && r.URL.Path != "/metrics" {
			log.Info("request", "correlation_id", id, "method", r.Method, "path", r.URL.Path, "status", rec.code, "ms", time.Since(start).Milliseconds())
		}
	})
}
