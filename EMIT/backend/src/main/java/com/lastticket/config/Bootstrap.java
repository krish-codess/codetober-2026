package com.lastticket.config;

import com.lastticket.inventory.InventoryService;
import com.lastticket.inventory.InventoryService.NewEvent;
import com.lastticket.inventory.InventoryService.NewInventory;
import io.swagger.v3.oas.models.Components;
import io.swagger.v3.oas.models.OpenAPI;
import io.swagger.v3.oas.models.info.Info;
import io.swagger.v3.oas.models.security.SecurityScheme;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.ApplicationRunner;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
class Bootstrap {
    private static final Logger log = LoggerFactory.getLogger(Bootstrap.class);
    /** Fixed so the generated attempt feed, the load test and the UI all agree on which event is the demo. */
    static final UUID DEMO_EVENT = UUID.fromString("00000000-0000-4000-8000-000000000001");

    /** 1,200 tickets across five sections. Goes through the normal write path, so opening stock is in the event stream. */
    @Bean
    ApplicationRunner seedDemo(AppProperties props, InventoryService inventory) {
        return args -> {
            if (!props.seedDemo()) {
                return;
            }
            boolean created = inventory.createEvent(new NewEvent(DEMO_EVENT, "The Last Ticket: Live at the Arena",
                    Instant.now().plusSeconds(props.seedOnSaleInSeconds()), 120, 4, 50, List.of(
                    new NewInventory("GA", "FLOOR", 9500, 400),
                    new NewInventory("GA", "LOWER", 7500, 300),
                    new NewInventory("GA", "UPPER", 4500, 380),
                    new NewInventory("VIP", "BOX", 32000, 40),
                    new NewInventory("VIP", "FLOOR", 22000, 80))));
            log.info(created ? "seeded demo event {}" : "demo event {} already present", DEMO_EVENT);
        };
    }

    @Bean
    OpenAPI openApi() {
        return new OpenAPI()
                .info(new Info().title("The Last Ticket API").version("1.0.0").description("""
                        On-sale ticketing: waiting room, holds with hard expiry, no oversell. \
                        Errors are RFC 9457 problem+json with a stable `code` and a `correlationId`."""))
                .components(new Components()
                        .addSecuritySchemes("session", new SecurityScheme().type(SecurityScheme.Type.HTTP).scheme("bearer").bearerFormat("JWT")
                                .description("Token from POST /api/sessions"))
                        .addSecuritySchemes("adminKey", new SecurityScheme().type(SecurityScheme.Type.APIKEY).in(SecurityScheme.In.HEADER).name("X-Admin-Key")));
    }
}
