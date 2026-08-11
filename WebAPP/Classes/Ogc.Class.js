import { Base } from "./Base.Class.js";

export class Ogc {

    static _request(type, path, data) {
        return new Promise((resolve, reject) => {
            $.ajax({
                url: Base.apiUrl() + path,
                async: true,
                type: type,
                dataType: 'json',
                contentType: data ? 'application/json' : undefined,
                data: data ? JSON.stringify(data) : undefined,
                credentials: 'include',
                xhrFields: { withCredentials: true },
                crossDomain: true,
                success: function (result) {
                    resolve(result);
                },
                error: function (xhr) {
                    let msg = (xhr.responseJSON && xhr.responseJSON.message)
                        || 'The OG-Core service could not be reached.';
                    reject(msg);
                }
            });
        });
    }

    //countries from the installer register, tagged with this machine's
    //install state; also carries catalog_source live | cache | none
    static getCalibrationCatalog() {
        return Ogc._request('GET', 'ogc/getCalibrationCatalog');
    }

    //calibrations on this machine, including custom ones not in the register
    static getInstalledCalibrations() {
        return Ogc._request('GET', 'ogc/getInstalledCalibrations');
    }

    //pre flight for the two custom sources, a local folder or a Git URL
    static checkCalibration(data) {
        return Ogc._request('POST', 'ogc/checkCalibration', data);
    }

    //install from the catalogue or a Git URL, returns an install_id to poll
    static installCalibration(data) {
        return Ogc._request('POST', 'ogc/installCalibration', data);
    }

    //adopt an existing local clone, returns an install_id to poll
    static registerLocalCalibration(data) {
        return Ogc._request('POST', 'ogc/registerLocalCalibration', data);
    }

    //progress of a running install or registration job
    static getInstallStatus(installId) {
        return Ogc._request('GET', 'ogc/getInstallStatus?install_id=' + encodeURIComponent(installId));
    }

    //check_only true compares against upstream, false applies the update
    static refreshCalibration(data) {
        return Ogc._request('POST', 'ogc/refreshCalibration', data);
    }

    //removes MUIOGO's record only, files on disk are kept
    static unregisterCalibration(countryId) {
        return Ogc._request('POST', 'ogc/unregisterCalibration', { country_id: countryId });
    }
}
