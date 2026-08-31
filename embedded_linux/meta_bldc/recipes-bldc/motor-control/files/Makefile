CC ?= cc
CPPFLAGS ?=
CFLAGS ?= -O2 -g
LDFLAGS ?=
LDLIBS ?=
INSTALL ?= install

PROJECT_CPPFLAGS := -D_POSIX_C_SOURCE=200809L
PROJECT_CFLAGS := -std=c11 -Wall -Wextra -Wpedantic -Werror \
	-Wshadow -Wformat=2 -Wundef -Wconversion -Wsign-conversion \
	-Wstrict-prototypes

PROGRAMS := motor-supervisor motorctl
TEST_PROGRAM := protocol-test

prefix ?= /usr
bindir ?= $(prefix)/bin
systemd_unitdir ?= $(prefix)/lib/systemd/system
DESTDIR ?=

.PHONY: all test install clean

all: $(PROGRAMS)

motor-supervisor: motor-supervisor.o protocol.o
	$(CC) $(LDFLAGS) -o $@ $^ $(LDLIBS)

motorctl: motorctl.o
	$(CC) $(LDFLAGS) -o $@ $^ $(LDLIBS)

$(TEST_PROGRAM): protocol-test.o protocol.o
	$(CC) $(LDFLAGS) -o $@ $^ $(LDLIBS)

%.o: %.c
	$(CC) $(CPPFLAGS) $(PROJECT_CPPFLAGS) $(CFLAGS) $(PROJECT_CFLAGS) -c -o $@ $<

motor-supervisor.o motorctl.o protocol.o protocol-test.o: protocol.h

test: $(TEST_PROGRAM)
	./$(TEST_PROGRAM)

install: all
	$(INSTALL) -d "$(DESTDIR)$(bindir)"
	$(INSTALL) -m 0755 motor-supervisor "$(DESTDIR)$(bindir)/motor-supervisor"
	$(INSTALL) -m 0755 motorctl "$(DESTDIR)$(bindir)/motorctl"
	$(INSTALL) -d "$(DESTDIR)$(systemd_unitdir)"
	$(INSTALL) -m 0644 motor-supervisor.service \
		"$(DESTDIR)$(systemd_unitdir)/motor-supervisor.service"

clean:
	$(RM) $(PROGRAMS) $(TEST_PROGRAM) *.o
