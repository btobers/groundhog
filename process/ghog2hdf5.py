#!/usr/bin/python3

import struct
import argparse
import os
import time
import calendar
import traceback

import numpy as np
import h5py
import glob
import matplotlib.pyplot as plt


def cli():
    # Command line interface
    parser = argparse.ArgumentParser(
        description="Convert Groundhog digitizer files to HDF5"
    )
    parser.add_argument(
        "files", type=str, help="Digitizer file(s) to convert to HDF5", nargs="+"
    )
    args = parser.parse_args()
    return args


def parseHeader(data, file):
    if data[0:4] != b"\xef\xbe\xd0\xd0":
        print(
            file,
            "is improperly formed Groundhog digitizer file, missing header segment magic bytes.",
        )
        return -1

    header = {}

    header["spt"] = struct.unpack("q", data[4:12])[0]
    header["pre_trig"] = struct.unpack("q", data[12:20])[0]
    header["prf"] = struct.unpack("q", data[20:28])[0]
    header["stack"] = struct.unpack("q", data[28:36])[0]
    header["trig"] = struct.unpack("h", data[36:38])[0]
    header["fs"] = struct.unpack("d", data[38:46])[0]

    return header


def parseTraces(data, spt, file):
    partial = False
    bpt = 8 * spt + 26  # bytes per trace

    if data[0:4] != b"\xce\xfa\xed\xfe":
        print(
            file,
            "is improperly formed Groundhog digitizer file, missing data segment magic bytes.",
        )
        return -1, -1

    if data[-4:] != b"\xad\xde\xad\xde":
        print(
            file,
            "is improperly formed Groundhog digitizer file, missing file end magic bytes.",
        )
        print("Will continue attempt to convert")
        partial = True

    ntrace = (len(data) - 8) / bpt

    if ntrace != int(ntrace):
        if partial:
            nkeep = int(ntrace) * bpt + 4
            data = data[:nkeep]
        else:
            print("File appears corrupted (some partial traces missing)")
            print("Need to implement reader for this")
            return -1, -1

    ntrace = int(ntrace)
    rx = np.zeros((spt, ntrace), dtype=np.int64)
    times = []

    data = data[4:]
    for i in range(ntrace):
        times.append(np.datetime64(data[i * bpt : i * bpt + 26].decode("utf-8")))
        rx[:, i] = struct.unpack("q" * spt, data[i * bpt + 26 : (i + 1) * bpt])

    return rx, times


def buildH5(header, rx, gps, file):
    try:
        fname = os.path.basename(file).replace(".ghog", "")
        if gps is not None:
            time0 = gps["utc"][0][:19].decode()
            time0 = time0.replace("-", "")
            time0 = time0.replace(":", "")
        else:
            time0 = "unk"
        outfile = os.path.dirname(file) + "/" + time0 + "_" + fname + ".h5"
        print("Saving ", outfile)

        fd = h5py.File(outfile, "w")

        raw = fd.create_group("raw")
        rx0 = raw.create_dataset("rx0", data=rx)

        if gps is not None:
            raw.create_dataset("gps0", data=gps)

        for k, v in header.items():
            rx0.attrs[k] = v

        fd.close()
    except Exception as e:
        print("Failure in buildH5")
        print(e)
        return -1

    return 0


def DDMtoDD(ddm):
    d = np.float64(ddm[:-7])
    m = np.float64(ddm[-7:]) / 60.0
    return d + m


def parseGPS(file):
    try:
        nmeas = open(file, mode="r").read().splitlines()
    except Exception as e:
        print(e)
        return -1, -1

    # List of tuples (system time, gnss time, lon, lat, hgt)
    lons = []
    lats = []
    hgts = []
    times = []
    dates = []
    for i, nmea in enumerate(nmeas):
        if "class" in nmea:
            # skip json info strings
            continue

        syst, fix = nmea.split(": $", maxsplit=1)
        syst = np.datetime64(syst)

        if "GPGGA" in fix:
            # Parse it
            fix = fix.split(",")
            time = (
                float(fix[1][:2]) * 60 * 60
                + float(fix[1][2:4]) * 60
                + float(fix[1][4:8])
            )
            lat = DDMtoDD(fix[2])
            lon = DDMtoDD(fix[4])
            hgt = float(fix[9])

            if fix[3] == "S":
                lat *= -1

            if fix[5] == "W":
                lon *= -1

            times.append((syst, time))
            lons.append((syst, lon))
            lats.append((syst, lat))
            hgts.append((syst, hgt))

            continue

        if "GPZDA" in fix:
            fix = fix.split(",")
            time = (
                float(fix[1][:2]) * 60 * 60
                + float(fix[1][2:4]) * 60
                + float(fix[1][4:8])
            )
            date = np.datetime64(fix[4] + "-" + fix[3] + "-" + fix[2])

            times.append((syst, time))
            dates.append((syst, date))

            continue

        if "GPRMC" in fix:
            fix = fix.split(",")
            time = (
                float(fix[1][:2]) * 60 * 60
                + float(fix[1][2:4]) * 60
                + float(fix[1][4:8])
            )

            times.append((syst, time))

            continue

    # Handle day rollover if necessary
    tsyst, tgps = zip(*times)
    tgps = list(tgps)
    if np.sum(np.diff(tgps) < 0):
        # Get system time of rollover
        roll = np.where(np.diff(tgps) < 0)[0][0] + 1
        for i in range(roll, len(tgps)):
            tgps[i] += 86400

    # Stitch dates and times
    try:
        _, dates = zip(*dates)
        dates = list(dates)
    except ValueError:
        # Handling weird ruth data
        print("Missing date information. Hardcoding to 2024-03-26")
        dates = [np.datetime64("2024-03-26")]

    # Handling other ruth issue
    if dates[0] > np.datetime64("2039"):
        print("Bad GPS week, removing 1024 weeks")
        dates[0] -= np.timedelta64(1024, "W")

    for i in range(len(tgps)):
        tgps[i] = dates[0] + np.timedelta64(int(tgps[i] * 1e3), "ms")

    times = list(zip(tsyst, tuple(tgps)))

    # Fill in location values if empty
    if lons == [] and lats == [] and hgts == []:
        print("No location values from gps")
        lons = list(zip(tsyst, np.zeros(len(tsyst))))
        lats = list(zip(tsyst, np.zeros(len(tsyst))))
        hgts = list(zip(tsyst, np.zeros(len(tsyst))))

    return {"lons": lons, "lats": lats, "hgts": hgts, "times": times}


def interpFix(tTrace, fix):
    # Interpolate GPS fix to trace times

    traceFix = {}
    warn = 0
    for k, v in fix.items():
        tFix, vals = zip(*v)
        epoch = tFix[0]
        if (tFix[0] > tTrace[0] or tFix[-1] < tTrace[-1]) and not warn:
            print("GPS times do not entirely contain data file times")
            warn = 1

        tTrace_sse = ((tTrace - epoch).astype("timedelta64[us]")).astype(
            np.float64
        ) / 1e6
        tFix_sse = ((tFix - epoch).astype("timedelta64[us]")).astype(np.float64) / 1e6

        if k == "times":
            # I think this will break if the file covers a leap second
            tUtc_sse = ((vals - epoch).astype("timedelta64[us]")).astype(
                np.float64
            ) / 1e6
            tUtc_sse_interp = np.interp(tTrace_sse, tFix_sse, tUtc_sse)
            traceFix[k] = epoch + (
                (tUtc_sse_interp * 1e6).astype(np.int64).astype("timedelta64[us]")
            )
        else:
            traceFix[k] = np.interp(tTrace_sse, tFix_sse, vals)

    return traceFix


def main():
    args = cli()

    for file in args.files:
        try:
            print("Converting " + file)
            try:
                fd = open(file, "rb")
            except Exception as e:
                print(e)
                continue

            data = fd.read()
            fd.close()

            if len(data) < 46:
                print(
                    "%s - Incomplete file, only partial header present. Skipping conversion"
                    % file
                )
                continue

            header = parseHeader(data, file)

            if header == -1:
                print("%s - Failed to parse file header" % file)
                continue

            rx, tTrace = parseTraces(data[46:], header["spt"], file)

            if tTrace == -1:
                print("%s - Failed to parse file data segment" % file)
                continue

            gpsFile = file.replace(".ghog", ".txt")
            if not os.path.isfile(gpsFile):
                print(
                    "%s - No GPS file found. No GPS information will be included in HDF5."
                    % file
                )
                fix = None
                gps = None
            else:
                fix = parseGPS(gpsFile)

                if fix == -1:
                    print(
                        "%s - Failed to parse GPS file. No GPS information will be included in HDF5."
                        % file
                    )
                    fix = None
                    gps = None
                else:
                    fix = interpFix(tTrace, fix)

            # get fix to right datattype for hdf5
            if fix is not None:
                gps_t = np.dtype(
                    [("lon", "f8"), ("lat", "f8"), ("hgt", "f8"), ("utc", "S26")]
                )
                gps = [None] * len(tTrace)
                for i in range(len(tTrace)):
                    gps[i] = tuple(
                        [
                            fix["lons"][i],
                            fix["lats"][i],
                            fix["hgts"][i],
                            np.datetime_as_string(fix["times"][i]),
                        ]
                    )
                gps = np.array(gps, dtype=gps_t)

            if buildH5(header, rx, gps, file) == -1:
                print("%s - Failed to build HDF5." % file)
                continue

            print()

        except Exception as e:
            print(e)
            print(traceback.format_exc())
            print("%s - Unanticipated failure. Skipping conversion." % file)

    return 0


main()
