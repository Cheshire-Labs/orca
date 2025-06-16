#pragma once
global resource Res_ML_STAR(1, 0xff0000, Translate("ML_STAR"));


function Res_ML_STAR_map(variable unit) variable { return(unit); }
function Res_ML_STAR_rmap(variable address) variable { return(address); }


namespace ResourceUnit {
     variable Res_ML_STAR;
}
// $$author=miike$$valid=0$$time=2025-01-07 20:39$$checksum=3225b4c2$$length=082$$