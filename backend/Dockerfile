# --- build: Maven + JDK, never shipped ---
FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /src
COPY pom.xml .
RUN --mount=type=cache,target=/root/.m2 mvn -q -B dependency:go-offline
COPY src ./src
RUN --mount=type=cache,target=/root/.m2 mvn -q -B package -DskipTests \
 && java -Djarmode=tools -jar target/last-ticket.jar extract --layers --destination /layers

# --- runtime: JRE only, non-root ---
FROM eclipse-temurin:17-jre-alpine
RUN addgroup -S app && adduser -S app -G app
WORKDIR /app
COPY --from=build /layers/dependencies/ ./
COPY --from=build /layers/spring-boot-loader/ ./
COPY --from=build /layers/snapshot-dependencies/ ./
COPY --from=build /layers/application/ ./
USER app
EXPOSE 8080 8081
ENTRYPOINT ["java", "-XX:MaxRAMPercentage=75", "-jar", "last-ticket.jar"]
