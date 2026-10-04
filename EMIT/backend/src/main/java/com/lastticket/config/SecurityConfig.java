package com.lastticket.config;

import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.List;
import org.slf4j.MDC;
import org.springframework.boot.actuate.autoconfigure.security.servlet.EndpointRequest;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpMethod;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ProblemDetail;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.security.oauth2.server.resource.web.authentication.BearerTokenAuthenticationFilter;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.web.filter.OncePerRequestFilter;

@Configuration
class SecurityConfig {

    @Bean
    SecurityFilterChain api(HttpSecurity http, Tokens tokens, AppProperties props, ObjectMapper json) throws Exception {
        http.csrf(c -> c.disable()) // no cookies: credentials travel in headers only
                .sessionManagement(s -> s.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
                .authorizeHttpRequests(a -> a
                        .requestMatchers(EndpointRequest.toAnyEndpoint()).permitAll() // management port only, not routed publicly
                        .requestMatchers(HttpMethod.POST, "/api/sessions").permitAll()
                        .requestMatchers(HttpMethod.GET, "/api/events", "/api/events/*", "/api/events/*/availability").permitAll()
                        .requestMatchers("/v3/api-docs/**", "/swagger-ui/**", "/swagger-ui.html", "/error").permitAll()
                        .requestMatchers("/api/admin/**").hasRole("ADMIN")
                        .anyRequest().authenticated())
                .oauth2ResourceServer(o -> o.jwt(j -> j.decoder(tokens.sessionDecoder()))
                        .authenticationEntryPoint((req, res, e) -> problem(res, json, HttpStatus.UNAUTHORIZED, "UNAUTHENTICATED",
                                "Missing, expired or invalid session token. POST /api/sessions for a new one."))
                        .accessDeniedHandler((req, res, e) -> problem(res, json, HttpStatus.FORBIDDEN, "FORBIDDEN", "You may not do that.")))
                .exceptionHandling(x -> x
                        .authenticationEntryPoint((req, res, e) -> problem(res, json, HttpStatus.UNAUTHORIZED, "UNAUTHENTICATED",
                                "Missing, expired or invalid session token. POST /api/sessions for a new one."))
                        .accessDeniedHandler((req, res, e) -> problem(res, json, HttpStatus.FORBIDDEN, "FORBIDDEN", "You may not do that.")))
                .addFilterBefore(new AdminKeyFilter(props.adminApiKey()), BearerTokenAuthenticationFilter.class);
        return http.build();
    }

    /** Same error shape as every other error in the API. */
    static void problem(HttpServletResponse res, ObjectMapper json, HttpStatus status, String code, String detail) throws IOException {
        ProblemDetail body = ProblemDetail.forStatusAndDetail(status, detail);
        body.setTitle(status.getReasonPhrase());
        body.setProperty("code", code);
        body.setProperty("correlationId", MDC.get("correlationId"));
        res.setStatus(status.value());
        res.setContentType(MediaType.APPLICATION_PROBLEM_JSON_VALUE);
        json.writeValue(res.getOutputStream(), body);
    }

    /** Operators authenticate with a static key from the environment. Compared in constant time. */
    static final class AdminKeyFilter extends OncePerRequestFilter {
        private final byte[] expected;

        AdminKeyFilter(String key) {
            this.expected = key.getBytes(StandardCharsets.UTF_8);
        }

        @Override
        protected void doFilterInternal(HttpServletRequest req, HttpServletResponse res, FilterChain chain) throws ServletException, IOException {
            String presented = req.getHeader("X-Admin-Key");
            if (presented != null && MessageDigest.isEqual(expected, presented.getBytes(StandardCharsets.UTF_8))) {
                SecurityContextHolder.getContext().setAuthentication(UsernamePasswordAuthenticationToken.authenticated(
                        "admin", null, List.of(new SimpleGrantedAuthority("ROLE_ADMIN"))));
            }
            chain.doFilter(req, res);
        }
    }
}
