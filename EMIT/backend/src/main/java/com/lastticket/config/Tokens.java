package com.lastticket.config;

import com.lastticket.api.ApiException;
import com.nimbusds.jose.jwk.source.ImmutableSecret;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.util.UUID;
import javax.crypto.SecretKey;
import javax.crypto.spec.SecretKeySpec;
import org.springframework.http.HttpStatus;
import org.springframework.security.oauth2.core.DelegatingOAuth2TokenValidator;
import org.springframework.security.oauth2.core.OAuth2Error;
import org.springframework.security.oauth2.core.OAuth2TokenValidatorResult;
import org.springframework.security.oauth2.jose.jws.MacAlgorithm;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtDecoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.security.oauth2.jwt.JwtException;
import org.springframework.security.oauth2.jwt.JwtTimestampValidator;
import org.springframework.security.oauth2.jwt.JwsHeader;
import org.springframework.security.oauth2.jwt.NimbusJwtDecoder;
import org.springframework.security.oauth2.jwt.NimbusJwtEncoder;
import org.springframework.stereotype.Component;

/**
 * HS256 tokens, two kinds, told apart by the {@code typ} claim so one can never be used as the other:
 * a session token (who you are; the bearer credential) and an admission token (the waiting room let this user in to
 * this event until then). Admission is a signed token rather than a Redis lookup so the purchase path has no
 * dependency on Redis.
 */
@Component
public class Tokens {
    static final Duration SESSION_TTL = Duration.ofHours(6);

    private final NimbusJwtEncoder encoder;
    private final NimbusJwtDecoder sessionDecoder;
    private final NimbusJwtDecoder admissionDecoder;

    public Tokens(AppProperties props) {
        SecretKey key = new SecretKeySpec(props.jwtSecret().getBytes(StandardCharsets.UTF_8), "HmacSHA256");
        this.encoder = new NimbusJwtEncoder(new ImmutableSecret<>(key));
        this.sessionDecoder = decoder(key, "session");
        this.admissionDecoder = decoder(key, "admission");
    }

    private static NimbusJwtDecoder decoder(SecretKey key, String typ) {
        NimbusJwtDecoder d = NimbusJwtDecoder.withSecretKey(key).macAlgorithm(MacAlgorithm.HS256).build();
        d.setJwtValidator(new DelegatingOAuth2TokenValidator<>(new JwtTimestampValidator(Duration.ZERO),
                jwt -> typ.equals(jwt.getClaimAsString("typ")) ? OAuth2TokenValidatorResult.success()
                        : OAuth2TokenValidatorResult.failure(new OAuth2Error("invalid_token", "wrong token type", null))));
        return d;
    }

    /** The decoder Spring Security uses for the Authorization header. */
    public JwtDecoder sessionDecoder() {
        return sessionDecoder;
    }

    public String session(String userId, Instant now) {
        return encode(JwtClaimsSet.builder().subject(userId).claim("typ", "session").issuedAt(now).expiresAt(now.plus(SESSION_TTL)).build());
    }

    public String admission(String userId, UUID eventId, Instant expiresAt) {
        return encode(JwtClaimsSet.builder().subject(userId).claim("typ", "admission").claim("evt", eventId.toString()).expiresAt(expiresAt).build());
    }

    /** Throws 403 unless {@code token} admits exactly this user to exactly this event, right now. */
    public void requireAdmission(String token, String userId, UUID eventId) {
        try {
            Jwt jwt = admissionDecoder.decode(token);
            if (userId.equals(jwt.getSubject()) && eventId.toString().equals(jwt.getClaimAsString("evt"))) {
                return;
            }
        } catch (JwtException e) {
            // fall through: expired, forged, malformed and wrong-type all get the same answer
        }
        throw new ApiException(HttpStatus.FORBIDDEN, "NOT_ADMITTED",
                "Your place in the queue has not come up, or your admission expired. Rejoin the queue.");
    }

    private String encode(JwtClaimsSet claims) {
        return encoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
    }
}
