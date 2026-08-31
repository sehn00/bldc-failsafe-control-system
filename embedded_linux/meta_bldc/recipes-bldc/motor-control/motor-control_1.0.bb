SUMMARY = "BLDC motor supervisor and command-line controller"
DESCRIPTION = "UART-owning motor supervisor and Unix-socket motorctl client"
LICENSE = "CLOSED"

SRC_URI = " \
    file://motor-supervisor.c \
    file://motorctl.c \
    file://protocol.c \
    file://protocol.h \
    file://Makefile \
    file://motor-supervisor.service \
"

S = "${WORKDIR}"

inherit features_check systemd

REQUIRED_DISTRO_FEATURES = "systemd"

SYSTEMD_SERVICE:${PN} = "motor-supervisor.service"
SYSTEMD_AUTO_ENABLE:${PN} = "enable"

do_compile() {
    oe_runmake CC="${CC}"
}

do_install() {
    oe_runmake install \
        DESTDIR="${D}" \
        bindir="${bindir}" \
        systemd_unitdir="${systemd_system_unitdir}"
}
