package com.lastticket.api;

import org.springframework.http.HttpStatus;

/** An error the caller can act on. {@code code} is the stable, machine-readable part of the contract. */
public class ApiException extends RuntimeException {
    private final HttpStatus status;
    private final String code;
    private final int retryAfterSeconds;

    public ApiException(HttpStatus status, String code, String message) {
        this(status, code, message, 0);
    }

    public ApiException(HttpStatus status, String code, String message, int retryAfterSeconds) {
        super(message);
        this.status = status;
        this.code = code;
        this.retryAfterSeconds = retryAfterSeconds;
    }

    public HttpStatus status() { return status; }
    public String code() { return code; }
    public int retryAfterSeconds() { return retryAfterSeconds; }
}
