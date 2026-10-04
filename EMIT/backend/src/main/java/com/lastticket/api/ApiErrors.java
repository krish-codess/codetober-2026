package com.lastticket.api;

import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.validation.ConstraintViolationException;
import java.io.IOException;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.regex.Pattern;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.MDC;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.dao.DataAccessException;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.HttpStatusCode;
import org.springframework.http.ProblemDetail;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Component;
import org.springframework.transaction.TransactionException;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.validation.method.ParameterErrors;
import org.springframework.validation.method.ParameterValidationResult;
import org.springframework.web.context.request.WebRequest;
import org.springframework.web.method.annotation.HandlerMethodValidationException;
import org.springframework.web.filter.OncePerRequestFilter;
import org.springframework.web.servlet.mvc.method.annotation.ResponseEntityExceptionHandler;

/**
 * One error shape for the whole API: RFC 9457 problem+json plus {@code code} (stable, machine-readable) and
 * {@code correlationId} (quote it in a bug report; it is on every log line of the request).
 */
@RestControllerAdvice
public class ApiErrors extends ResponseEntityExceptionHandler {
    private static final Logger log = LoggerFactory.getLogger(ApiErrors.class);

    @ExceptionHandler(ApiException.class)
    ResponseEntity<Object> api(ApiException e, WebRequest request) {
        HttpHeaders headers = new HttpHeaders();
        if (e.retryAfterSeconds() > 0) {
            headers.set(HttpHeaders.RETRY_AFTER, Integer.toString(e.retryAfterSeconds()));
        }
        ProblemDetail body = ProblemDetail.forStatusAndDetail(e.status(), e.getMessage());
        body.setProperty("code", e.code());
        return handleExceptionInternal(e, body, headers, e.status(), request);
    }

    @ExceptionHandler(ConstraintViolationException.class)
    ResponseEntity<Object> constraint(ConstraintViolationException e, WebRequest request) {
        return handleExceptionInternal(e, ProblemDetail.forStatusAndDetail(HttpStatus.BAD_REQUEST, e.getMessage()), new HttpHeaders(),
                HttpStatus.BAD_REQUEST, request);
    }

    /** Postgres unreachable, pool exhausted, statement timeout: say so, and say when to come back. */
    @ExceptionHandler({DataAccessException.class, TransactionException.class})
    ResponseEntity<Object> dependency(Exception e, WebRequest request) {
        log.error("dependency failure", e);
        HttpHeaders headers = new HttpHeaders();
        headers.set(HttpHeaders.RETRY_AFTER, "2");
        ProblemDetail body = ProblemDetail.forStatusAndDetail(HttpStatus.SERVICE_UNAVAILABLE,
                "We could not reach our database. Nothing was charged or reserved by this request; please try again shortly.");
        body.setProperty("code", "DEPENDENCY_UNAVAILABLE");
        return handleExceptionInternal(e, body, headers, HttpStatus.SERVICE_UNAVAILABLE, request);
    }

    @ExceptionHandler(Exception.class)
    ResponseEntity<Object> unexpected(Exception e, WebRequest request) {
        log.error("unhandled exception", e);
        ProblemDetail body = ProblemDetail.forStatusAndDetail(HttpStatus.INTERNAL_SERVER_ERROR,
                "Something went wrong on our side. Quote the correlationId if you report this.");
        body.setProperty("code", "INTERNAL");
        return handleExceptionInternal(e, body, new HttpHeaders(), HttpStatus.INTERNAL_SERVER_ERROR, request);
    }

    @Override
    protected ResponseEntity<Object> handleMethodArgumentNotValid(MethodArgumentNotValidException e, HttpHeaders headers,
                                                                  HttpStatusCode status, WebRequest request) {
        ProblemDetail body = e.getBody();
        body.setDetail("Request body failed validation.");
        List<Map<String, String>> errors = e.getBindingResult().getFieldErrors().stream()
                .map(f -> Map.of("field", f.getField(), "message", String.valueOf(f.getDefaultMessage()))).toList();
        body.setProperty("errors", errors);
        return handleExceptionInternal(e, body, headers, status, request);
    }

    /** Same shape as above for failures on headers, query and path parameters. */
    @Override
    protected ResponseEntity<Object> handleHandlerMethodValidationException(HandlerMethodValidationException e, HttpHeaders headers,
                                                                            HttpStatusCode status, WebRequest request) {
        List<Map<String, String>> errors = new java.util.ArrayList<>();
        for (ParameterValidationResult result : e.getParameterValidationResults()) {
            if (result instanceof ParameterErrors body) {
                body.getFieldErrors().forEach(f -> errors.add(Map.of("field", f.getField(), "message", String.valueOf(f.getDefaultMessage()))));
            } else {
                String name = String.valueOf(result.getMethodParameter().getParameterName());
                result.getResolvableErrors().forEach(r -> errors.add(Map.of("field", name, "message", String.valueOf(r.getDefaultMessage()))));
            }
        }
        ProblemDetail body = e.getBody();
        body.setDetail("Request failed validation.");
        body.setProperty("errors", errors);
        return handleExceptionInternal(e, body, headers, status, request);
    }

    @Override
    protected ResponseEntity<Object> handleExceptionInternal(Exception e, Object body, HttpHeaders headers, HttpStatusCode status, WebRequest request) {
        ResponseEntity<Object> response = super.handleExceptionInternal(e, body, headers, status, request);
        if (response != null && response.getBody() instanceof ProblemDetail pd) {
            if (pd.getProperties() == null || !pd.getProperties().containsKey("code")) {
                HttpStatus resolved = HttpStatus.resolve(status.value());
                pd.setProperty("code", status.value() == 400 ? "INVALID_REQUEST" : resolved == null ? "ERROR" : resolved.name());
            }
            pd.setProperty("correlationId", MDC.get("correlationId"));
        }
        return response;
    }

    /**
     * Accepts a caller-supplied X-Request-Id (if it looks sane) or mints one; puts it in the logging context and on
     * the response. Runs before security so that rejected requests are traceable too.
     */
    @Component
    @Order(Ordered.HIGHEST_PRECEDENCE)
    static class CorrelationFilter extends OncePerRequestFilter {
        private static final Pattern SANE = Pattern.compile("[A-Za-z0-9._-]{8,64}");

        @Override
        protected void doFilterInternal(HttpServletRequest req, HttpServletResponse res, FilterChain chain) throws ServletException, IOException {
            String id = req.getHeader("X-Request-Id");
            if (id == null || !SANE.matcher(id).matches()) {
                id = UUID.randomUUID().toString();
            }
            MDC.put("correlationId", id);
            res.setHeader("X-Request-Id", id);
            try {
                chain.doFilter(req, res);
            } finally {
                MDC.remove("correlationId");
            }
        }
    }
}
