/* SPDX-License-Identifier: GPL-2.0-only OR MIT */
/*
 * Shared helpers for the SLASH kernel-module kselftests.
 *
 * Header-only.  Each test binary includes this file and inlines what it
 * needs.  Device paths are hardcoded to /dev/slash_ctl0 and
 * /dev/slash_qdma_ctl0 — the spec at docs/reference/kernel-abi/index.rst
 * warns that those suffixes are independent across the two device
 * categories, so any test that opens both fds calls
 * slash_assert_same_card() to verify they describe the same physical card.
 */

#ifndef SLASH_TEST_HELPERS_H
#define SLASH_TEST_HELPERS_H

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <linux/dma-buf.h>
#include <sys/ioctl.h>

#include <slash/uapi/slash_interface.h>

#define SLASH_TEST_CTL_DEV "/dev/slash_ctl0"
#define SLASH_TEST_QDMA_DEV "/dev/slash_qdma_ctl0"
#define SLASH_TEST_HOTPLUG_DEV "/dev/slash_hotplug"

/* Documented PCI identity for SLASH cards. */
#define SLASH_TEST_VENDOR_ID 0x10EE
#define SLASH_TEST_PF1_DEV_ID 0x50B5 /* QDMA */
#define SLASH_TEST_PF2_DEV_ID 0x50B6 /* Control */

/*
 * 16 TB NoC interconnect address map regions, from
 * docs/reference/kernel-abi/index.rst.
 */
#define SLASH_TEST_HBM_BASE 0x0000004000000000ULL
#define SLASH_TEST_HBM_END 0x0000004800000000ULL
#define SLASH_TEST_DDR_BASE 0x0000060000000000ULL
#define SLASH_TEST_DDR_END 0x0000060800000000ULL
#define SLASH_TEST_BITSTREAM_BASE 0x0000000102100000ULL
#define SLASH_TEST_BITSTREAM_END 0x0000000142100000ULL

/* Length (incl. NUL) of the "DDDD:BB:SS" bus prefix shared by all PFs of a card. */
#define SLASH_TEST_BUS_PREFIX_LEN 10

/**
 * slash_get_device_info() - Issue GET_DEVICE_INFO on a control fd.
 * @fd:  An open /dev/slash_ctl<N> or /dev/slash_qdma_ctl<N> fd.
 * @out: Caller-owned struct; size field is set by this helper.
 *
 * Return: 0 on success, -errno on failure.
 */
static inline int slash_get_device_info(int fd, struct slash_ioctl_device_info *out)
{
	memset(out, 0, sizeof(*out));
	out->size = sizeof(*out);
	if (ioctl(fd, SLASH_CTLDEV_IOCTL_GET_DEVICE_INFO, out) < 0)
		return -errno;
	return 0;
}

/**
 * slash_get_bdf() - Read the BDF string for an open control fd.
 *
 * Return: 0 on success, -errno on failure.  @bdf_out must be at least
 * SLASH_PCI_BDF_LEN bytes.
 */
static inline int slash_get_bdf(int fd, char bdf_out[SLASH_PCI_BDF_LEN])
{
	struct slash_ioctl_device_info info;
	int err = slash_get_device_info(fd, &info);

	if (err)
		return err;
	memcpy(bdf_out, info.bdf, SLASH_PCI_BDF_LEN);
	bdf_out[SLASH_PCI_BDF_LEN - 1] = '\0';
	return 0;
}

/**
 * slash_same_card() - Do two BDFs share a "DDDD:BB:SS" prefix?
 *
 * Return: 1 if yes, 0 if no.
 */
static inline int slash_same_card(const char *bdf_a, const char *bdf_b)
{
	/* "DDDD:BB:SS" is 10 chars, excluding the trailing ".F" and NUL. */
	return strncmp(bdf_a, bdf_b, SLASH_TEST_BUS_PREFIX_LEN - 1) == 0;
}

/**
 * slash_find_first_mmio_bar() - Iterate BARs 0..5, return first usable.
 *
 * Return: 0 on success (writes @bar_out and @len_out),
 *         -ENOENT if no usable BAR exists, -errno on ioctl failure.
 */
static inline int slash_find_first_mmio_bar(int ctl_fd, uint8_t *bar_out,
											uint64_t *len_out)
{
	int i;

	for (i = 0; i < 6; i++)
	{
		struct slash_ioctl_bar_info info;

		memset(&info, 0, sizeof(info));
		info.size = sizeof(info);
		info.bar_number = i;
		if (ioctl(ctl_fd, SLASH_CTLDEV_IOCTL_GET_BAR_INFO, &info) < 0)
			return -errno;
		if (info.usable)
		{
			*bar_out = i;
			*len_out = info.length;
			return 0;
		}
	}
	return -ENOENT;
}

/**
 * slash_find_unusable_bar() - First BAR with usable == 0, or -ENOENT.
 */
static inline int slash_find_unusable_bar(int ctl_fd, uint8_t *bar_out)
{
	int i;

	for (i = 0; i < 6; i++)
	{
		struct slash_ioctl_bar_info info;

		memset(&info, 0, sizeof(info));
		info.size = sizeof(info);
		info.bar_number = i;
		if (ioctl(ctl_fd, SLASH_CTLDEV_IOCTL_GET_BAR_INFO, &info) < 0)
			return -errno;
		if (!info.usable)
		{
			*bar_out = i;
			return 0;
		}
	}
	return -ENOENT;
}

/**
 * slash_dma_buf_sync() - Wrap DMA_BUF_IOCTL_SYNC.
 */
static inline int slash_dma_buf_sync(int bar_fd, uint64_t flags)
{
	struct dma_buf_sync sync = {.flags = flags};

	if (ioctl(bar_fd, DMA_BUF_IOCTL_SYNC, &sync) < 0)
		return -errno;
	return 0;
}

/**
 * slash_qpair_add() - Convenience wrapper for QPAIR_ADD.
 *
 * Return: 0 on success (writes @qid_out), -errno on failure.
 */
static inline int slash_qpair_add(int qdma_fd, uint32_t mode, uint32_t dir_mask,
								  uint32_t *qid_out)
{
	struct slash_qdma_qpair_add add;

	memset(&add, 0, sizeof(add));
	add.size = sizeof(add);
	add.mode = mode;
	add.dir_mask = dir_mask;
	if (ioctl(qdma_fd, SLASH_QDMA_IOCTL_QPAIR_ADD, &add) < 0)
		return -errno;
	*qid_out = add.qid;
	return 0;
}

/**
 * slash_qpair_op() - Convenience wrapper for Q_OP.
 */
static inline int slash_qpair_op(int qdma_fd, uint32_t qid, uint32_t op)
{
	struct slash_qdma_qpair_op req;

	memset(&req, 0, sizeof(req));
	req.size = sizeof(req);
	req.qid = qid;
	req.op = op;
	if (ioctl(qdma_fd, SLASH_QDMA_IOCTL_Q_OP, &req) < 0)
		return -errno;
	return 0;
}

/**
 * slash_qpair_get_fd() - Convenience wrapper for QPAIR_GET_FD.
 *
 * Return: new fd (>= 0) on success, -errno on failure.
 */
static inline int slash_qpair_get_fd(int qdma_fd, uint32_t qid, uint32_t flags)
{
	struct slash_qdma_qpair_fd_request req;
	int ret;

	memset(&req, 0, sizeof(req));
	req.size = sizeof(req);
	req.qid = qid;
	req.flags = flags;
	ret = ioctl(qdma_fd, SLASH_QDMA_IOCTL_QPAIR_GET_FD, &req);
	if (ret < 0)
		return -errno;
	return ret;
}

#endif /* SLASH_TEST_HELPERS_H */
