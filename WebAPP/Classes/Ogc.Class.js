import { Base } from "./Base.Class.js";

export class Ogc {

    // Available country calibrations from the OG-Core installer register,
    // each tagged with this machine's install state. Response also carries
    // catalog_source: live | cache | none.
    static getCalibrationCatalog() {
        return new Promise((resolve, reject) => {
            $.ajax({
                url: Base.apiUrl() + "ogc/getCalibrationCatalog",
                async: true,
                type: 'GET',
                dataType: 'json',
                credentials: 'include',
                xhrFields: { withCredentials: true },
                crossDomain: true,
                success: function (result) {
                    resolve(result);
                },
                error: function (xhr) {
                    reject("The calibration catalogue could not be loaded.");
                }
            });
        });
    }
}
