---
name: java-kotlin
description: Java and Kotlin on the JVM - JDK selection, Gradle (Kotlin DSL) and Maven builds, project structure, dependency management, Spring Boot services, Kotlin coroutines, testing (JUnit 5), packaging (fat jars, jlink/jpackage), and diagnosing JVM issues (heap, threads, GC). Use for any Java/Kotlin/JVM project, including Android build issues and Minecraft server/plugin work.
keywords: java, kotlin, gradle, maven, jvm, spring boot, android studio
---

# Java and Kotlin

## JDK
- Use an LTS JDK (17 or 21; Temurin builds). Check `java -version` and `JAVA_HOME`; Gradle uses the toolchain declared in the build (`java { toolchain { languageVersion = JavaLanguageVersion.of(21) } }`).
- Multiple JDKs on Windows: set `JAVA_HOME` per command, e.g. `set JAVA_HOME=C:\...\jdk-21&& gradlew build`.

## Builds
- Always use the wrapper in the repo: `./gradlew build` / `gradlew.bat build`, `./mvnw package`. Do not install a global Gradle/Maven version to "fix" builds.
- Gradle: `./gradlew tasks`, `./gradlew test --tests 'pkg.ClassTest'`, `./gradlew dependencies --configuration runtimeClasspath`, `--stacktrace`/`--info` for failures, `--no-daemon` in CI. Version catalogs in `gradle/libs.versions.toml`.
- Maven: `./mvnw -B -ntp verify` (batch mode, no transfer progress), `dependency:tree`.
- Dependency conflicts: inspect the tree, then align with a BOM/platform (`implementation(platform("..."))`) instead of excluding randomly.

## Spring Boot
`./gradlew bootRun`; configuration in `application.yml` with profiles; constructor injection; `@ConfigurationProperties` for typed config; Actuator for health (`/actuator/health`). Build an image with `./gradlew bootBuildImage` or a Dockerfile on a JRE base.

## Kotlin
- Null safety: avoid `!!`; use `?.`, `?:`, `requireNotNull` with a message. Data classes for values, sealed interfaces for state.
- Coroutines: structured concurrency (`coroutineScope`, `supervisorScope`), `Dispatchers.IO` for blocking I/O, never `GlobalScope`; cancellation is cooperative.

## Testing
JUnit 5 (`@Test`, `@ParameterizedTest`), AssertJ or kotest assertions, MockK (Kotlin) / Mockito (Java), Testcontainers for real databases.

## Packaging
Fat jar (Spring Boot jar, Shadow plugin), `jlink` for a trimmed runtime, `jpackage` for native installers (`jpackage --input build/libs --main-jar app.jar --type msi`).

## JVM diagnosis
- Memory: `-Xmx` sized below container/host limits; heap dump on OOM `-XX:+HeapDumpOnOutOfMemoryError`; inspect with Eclipse MAT/VisualVM.
- Threads/hangs: `jcmd <pid> Thread.print` (or `jstack`), GC logs `-Xlog:gc*:file=gc.log`. List JVMs with `jcmd`.
- Minecraft servers: Paper/Purpur with Aikar's flags style G1 settings, allocate `-Xms` = `-Xmx`, watch TPS/MSPT (`/spark profiler` if installed), plugins built against the server's API version.
