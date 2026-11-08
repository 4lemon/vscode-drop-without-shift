/*
 * The program inside "VS Code Drop Fix.app" - the launcher for the launchd agent.
 *
 * Why: macOS lets only a program holding the "App Management" permission modify a signed
 * app (VS Code). Granting it to python or node is unsafe: any of their scripts could then
 * modify any app. So this tiny app gets the permission and runs python as its child
 * process; macOS keeps treating the app as the responsible process.
 *
 * The command is baked in at build time (install.py, macros PYTHON and SCRIPT) and the
 * program accepts no arguments: otherwise an app holding this permission could be made
 * to run anything.
 */
#include <errno.h>
#include <spawn.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>

#ifndef PYTHON
#error "PYTHON is not defined: build through install.py"
#endif
#ifndef SCRIPT
#error "SCRIPT is not defined: build through install.py"
#endif

extern char **environ;

int main(void) {
    char *argv[] = {PYTHON, "-I", SCRIPT, "--auto", NULL};
    pid_t pid;
    int rc = posix_spawn(&pid, argv[0], NULL, NULL, argv, environ);
    if (rc != 0) {
        fprintf(stderr, "vscode-drop-fix: could not start %s: %s\n", argv[0], strerror(rc));
        return 1;
    }
    int status;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) {
            perror("vscode-drop-fix: waitpid");
            return 1;
        }
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
